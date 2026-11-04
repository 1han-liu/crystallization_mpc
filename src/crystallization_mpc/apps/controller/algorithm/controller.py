"""Stateful crystallization control algorithm."""

from __future__ import annotations

import copy
import logging
from dataclasses import replace
from typing import Any, Mapping

import numpy as np

from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.diagnostics import ControllerDiagnosticsSnapshot
from crystallization_mpc.apps.controller.process import (ControllerReadError, ProcessState, MATLAB_EMPTY, MATLAB_EMPTY_CELL, MATLAB_UNDEFINED, is_matlab_empty, matlab_value_document)
from crystallization_mpc.apps.controller.tick import ControllerGrowthMeasurementError, ControllerInputValueError, ControllerTickInput
from ._logical import source_if, source_not
from crystallization_mpc.apps.controller.numeric import decode_nonfinite
from crystallization_mpc.messaging.parameter_policy import (
    parameter_defaults, derived_parameter_policies,
)

from .adaptation import AdaptationError, adapt_growth_parameters
from ._matlab_random import MATLABRandom
from ._checkpoint import validate_checkpoint
from .control import (
    OptimizationError,
    calc_T_j_set,
    calc_dT_dt_set,
    calc_t_lag_perc,
    objective_function,
    update_T_j,
)
from .dynamics import state_transition_function, state_transition_function_T
from .ekf import (
    ExtendedKalmanFilter,
    construct_EKF,
    create_EKF_general,
    restore_EKF,
    smooth_EKF,
    smooth_EKF_general,
    measurement_function,
)
from .mass_balance import calc_next_crystallization_mass_balance, create_size_list
from .parameters import parameter_digest
from .thermodynamics import calc_G, calc_relative_sigma, calc_sigma


BASELINE_COMMIT = "ce885a13e0a3e95ac0509e06eebf9d7cd1d418b0"
logger = logging.getLogger(__name__)

HISTORY_KEYS = (
    "t", "T", "T_KF", "T_j_set", "T_j", "dT_dt", "dT_dt_KF",
    "dT_dt_set", "c", "c_KF", "dc_dt", "dc_dt_KF", "target",
    "target_set", "abs_e_target", "objective", "target_sigma",
    "target_sigma_set", "abs_e_target_sigma", "objective_sigma", "target_G",
    "target_G_set", "abs_e_target_G", "objective_G", "sigma", "G_model",
    "G_u", "G_u_KF", "G_v", "G_v_KF", "G_measure", "G_measure_KF",
    "count_middle", "n", "k_0", "E_A", "t_lag", "t_lag_perc", "to_adapt",
    "int_e_target_dt", "e_dT_dt", "n_KF", "k_0_KF", "E_A_KF",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return _json_value(value.tolist())
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


class CrystallizationController:
    """Own the numerical state for one configured experiment."""

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}
        self._configured_params: dict[str, Any] = {}
        self.params_digest: str | None = None
        self.run_id: str | None = None
        self.configured = False
        self.running = False
        self.adaptation_enabled = MATLAB_UNDEFINED
        self.adaptation_mode = MATLAB_UNDEFINED
        self._reset_runtime_state()
        self._root_workspace: dict[str, Any] = {}
        self._model_params: dict[str, Any] = {}
        self._filter_bindings: dict[str, Any] = {}
        self._filter_dts: dict[str, Any] = {}
        self.controller_refresh_request = False
        self.G_active = MATLAB_UNDEFINED
        self.rng = MATLABRandom(0)
        self._pending_refresh_initial_snapshot = None

    def _reset_runtime_state(self) -> None:
        self.frame_index = 0
        self.elapsed_s = 0.0
        self._history_tick_seq: list[int] = []
        self._history_assigned: dict[str, list[bool]] = {key: [] for key in HISTORY_KEYS}
        self._read_failures: list[dict[str, Any]] = []
        self._runtime_parameter_ledger: list[dict[str, Any]] = []
        self._last_parameter_event: dict[str, Any] | None = None
        self._seed_population: dict[str, Any] = {"initial_count": 0, "base_seeded": False, "additions": []}
        self.history: dict[str, list[Any]] = {key: [] for key in HISTORY_KEYS}
        for key, values in self.history.items():
            setattr(self, f"{key}_list", values)
        self.int_e_target_dt = MATLAB_UNDEFINED
        self.int_e_dT_dt_dt = MATLAB_UNDEFINED
        self.int_e_T_dt = MATLAB_UNDEFINED
        self.num_adapt = 0
        self.pending_seed = False
        self.mark_seed = False
        self.size_list_seed = np.empty(0, dtype=float)
        self.size_list = np.empty(0, dtype=float)
        self._seed_generation_completed = False
        self.simulation_state: dict[str, float] | None = None
        self.control_T_j_reference_K: float | None = None
        self._activation_read_failure: dict[str, Any] | None = None
        # Numerical indexed views reproduce the source derivative window,
        # including unassigned slots. Display history and fitting segments
        # retain their separate identities.
        self.fitting_G_measure_history: list[float] = []
        self.fitting_segment: dict[str, list[Any]] = {
            "G": [], "sigma": [], "T": [], "ticks": [],
        }
        self.fitting_last_tick: int | None = None
        self.segment_id = 0
        self.segment_start_tick = 0
        self.segment_history_start = 0
        self.refresh_count = 0
        self.last_refresh_event: dict[str, Any] | None = None
        self.ekf: ExtendedKalmanFilter | None = None
        self.ekf_G_measure: ExtendedKalmanFilter | None = None
        self.ekf_n: ExtendedKalmanFilter | None = None
        self.ekf_k_0: ExtendedKalmanFilter | None = None
        self.ekf_E_A: ExtendedKalmanFilter | None = None
        self.last_adaptation_pause_reason: str | None = None
        self.last_control_hold_reason: str | None = None
        self.adaptation_diagnostics: dict[str, Any] = {
            "last_status": "not_run", "fit_count": 0, "failure_count": 0,
            "last_tick": None, "last_mode": None,
        }
        # Diagnostics are transient copies, never algorithm recovery state.
        self._diagnostic_tick_data: dict[str, Any] | None = None
        self._diagnostic_snapshot: ControllerDiagnosticsSnapshot | None = None
        self._diagnostic_capture_failed = False

    def _diagnostic_filters(self) -> dict[str, ExtendedKalmanFilter | None]:
        return {
            "main": self.ekf, "G": self.ekf_G_measure, "E_A": self.ekf_E_A,
            "k_0": self.ekf_k_0, "n": self.ekf_n,
        }

    def _attach_diagnostic_observers(self) -> None:
        try:
            for name, filter_ in self._diagnostic_filters().items():
                if filter_ is not None:
                    filter_.diagnostic_capture_failed = False
                    filter_.diagnostic_observer = (
                        lambda stage, values, name=name: self._capture_filter_diagnostics(name, stage, values)
                    )
        except Exception:
            self._diagnostic_capture_failed = True

    def _capture_filter_diagnostics(
        self, name: str, stage: str, values: Mapping[str, Any]
    ) -> None:
        try:
            if self._diagnostic_tick_data is None:
                return
            record = self._diagnostic_tick_data["filters"].setdefault(name, {})
            record[stage] = {
                key: value.tolist() if isinstance(value, np.ndarray) else copy.deepcopy(value)
                for key, value in values.items()
            }
        except Exception:
            self._diagnostic_capture_failed = True

    def _capture_control_diagnostics(self, values: Mapping[str, Any]) -> None:
        try:
            if self._diagnostic_tick_data is not None:
                self._diagnostic_tick_data["control"].update(values)
        except Exception:
            self._diagnostic_capture_failed = True

    def _capture_initial_diagnostics(self) -> None:
        try:
            self._attach_diagnostic_observers()
            if self._diagnostic_tick_data is not None:
                initialized = {
                    name: {"state": filter_.state.tolist(), "covariance": filter_.covariance.tolist()}
                    for name, filter_ in self._diagnostic_filters().items()
                    if filter_ is not None
                }
                # Freeze at the initialization boundary, not only when the
                # cycle's final diagnostic snapshot is assembled later.
                self._diagnostic_tick_data["refresh_init"] = ControllerDiagnosticsSnapshot.from_data(
                    tick_seq=self.frame_index, elapsed_s=self.elapsed_s, run_id=str(self.run_id),
                    fields={"diag_snapshot_stage": "refresh_init"},
                    raw={"refresh_init": initialized},
                )
        except Exception:
            self._diagnostic_capture_failed = True

    def diagnostics_snapshot(self) -> ControllerDiagnosticsSnapshot | None:
        """Return the last completed observational snapshot."""
        return self._diagnostic_snapshot

    def _finish_diagnostics(
        self, *, T: float, c: float, T_j: float, G_measure: float | None,
        refreshed: bool, growth_updated: bool, growth_reanchored: bool,
        projected: np.ndarray, dT_dt_request: float | None, T_j_set: float,
    ) -> None:
        try:
            if (self._diagnostic_capture_failed or self._diagnostic_tick_data is None
                    or any(filter_.diagnostic_capture_failed for filter_ in self._diagnostic_filters().values()
                           if filter_ is not None)):
                self._diagnostic_snapshot = None
                return
            data = self._diagnostic_tick_data
            fields: dict[str, int | float | str | bool] = {
                "diag_snapshot_stage": "refresh_init" if refreshed else "posterior",
                "diag_refresh_applied": refreshed,
                "diag_refresh_count": int(self.refresh_count),
                "diag_segment_id": int(self.segment_id),
                "diag_segment_start_tick": int(self.segment_start_tick),
                "diag_sample_count": sum(value > 0 for value in self.fitting_segment["G"]),
                "diag_minimum_samples": int(self.params["min_num_adapt"]),
                "diag_fit_count": int(self.adaptation_diagnostics["fit_count"]),
                "diag_fit_failures": int(self.adaptation_diagnostics["failure_count"]),
                "diag_main_T_measured": T, "diag_main_c_measured": c,
                "diag_int_target": float(self.int_e_target_dt),
                "diag_int_dT_dt": float(self.int_e_dT_dt_dt),
                "diag_int_T": float(self.int_e_T_dt),
                "diag_T_j_actual": T_j, "diag_T_j_selected": T_j_set,
                "diag_control_hold_reason": str(data["control"].get("hold_reason", self.last_control_hold_reason or "")),
                "diag_physical_limit": bool(data["control"].get("physical_limit", False)),
                "diag_non_increasing_limit": bool(data["control"].get("non_increasing_limit", False)),
                "diag_T_j_selected_branch": str(data["control"].get("selected_branch", "held")),
            }
            if data["control"].get("request_physical_limit") is not None:
                fields["diag_request_physical_limit"] = bool(data["control"]["request_physical_limit"])
            if data["control"].get("zero_physical_limit") is not None:
                fields["diag_zero_physical_limit"] = bool(data["control"]["zero_physical_limit"])
            if dT_dt_request is not None:
                fields["diag_dT_dt_request"] = dT_dt_request
            for index, label in enumerate(("T", "dT_dt", "c", "dc_dt")):
                fields[f"diag_projected_{label}"] = float(projected[index])
            lifecycle = {
                "main": "refresh_init" if refreshed else "updated",
                "G": ("refresh_init" if refreshed else
                      "reanchored_updated" if growth_updated and growth_reanchored else
                      "updated" if growth_updated else "paused"),
                **{name: "refresh_reserved" if refreshed else "reserved" for name in ("E_A", "k_0", "n")},
            }
            raw_filters: dict[str, Any] = {}
            for name, filter_ in self._diagnostic_filters().items():
                if filter_ is None or filter_._diagnostic_initial_covariance is None:
                    raise ValueError("Missing filter diagnostic initialization.")
                fields[f"diag_{name}_lifecycle"] = lifecycle[name]
                observed = data["filters"].get(name, {})
                raw_filters[name] = {
                    **observed, "state": filter_.state.tolist(),
                    "covariance": filter_.covariance.tolist(),
                    "initial_covariance": filter_._diagnostic_initial_covariance.tolist(),
                }
                if name == "main":
                    for index, label in enumerate(("T", "dT_dt", "c", "dc_dt")):
                        fields[f"diag_main_{label}_state"] = float(filter_.state[index])
                        fields[f"diag_main_P_{label}"] = float(filter_.covariance[index, index])
                        fields[f"diag_main_P_{label}_initial"] = float(filter_._diagnostic_initial_covariance[index, index])
                    prior = observed.get("prior")
                    if prior is not None:
                        fields["diag_main_T_prior"] = float(prior["state"][0])
                        fields["diag_main_c_prior"] = float(prior["state"][2])
                    posterior = observed.get("posterior")
                    if posterior is not None:
                        for index, label in enumerate(("T", "c")):
                            fields[f"diag_main_innovation_{label}"] = float(posterior["innovation"][index])
                            fields[f"diag_main_S_{label}"] = float(posterior["innovation_covariance"][index][index])
                elif name == "G":
                    for index, label in enumerate(("G", "dG_dt", "d2G_dt2")):
                        fields[f"diag_G_P_{label}"] = float(filter_.covariance[index, index])
                        fields[f"diag_G_P_{label}_initial"] = float(filter_._diagnostic_initial_covariance[index, index])
                    if G_measure is not None:
                        fields["diag_G_input"] = G_measure
                    if growth_updated:
                        fields["diag_G_state"] = float(filter_.state[0])
                        prior = observed.get("prior")
                        if prior is not None:
                            fields["diag_G_prior"] = float(prior["state"][0])
                        posterior = observed.get("posterior")
                        if posterior is not None:
                            fields["diag_G_innovation_G"] = float(posterior["innovation"][0])
                            fields["diag_G_S_G"] = float(posterior["innovation_covariance"][0][0])
            for source, target in (
                ("candidate_raw", "diag_T_j_candidate_raw"),
                ("candidate_bounded", "diag_T_j_candidate_bounded"),
                ("zero_raw", "diag_T_j_zero_raw"),
                ("zero_bounded", "diag_T_j_zero_bounded"),
            ):
                value = data["control"].get(source)
                if value is not None:
                    fields[target] = float(value)
            self._diagnostic_snapshot = ControllerDiagnosticsSnapshot.from_data(
                tick_seq=self.frame_index, elapsed_s=self.elapsed_s, run_id=str(self.run_id),
                fields=fields,
                raw={
                    "filters": raw_filters,
                    "refresh_init": (data["refresh_init"].raw_data["refresh_init"]
                                     if data["refresh_init"] is not None else None),
                    "lifecycle": lifecycle, "control": {
                        **data["control"], "selected_branch": fields["diag_T_j_selected_branch"],
                        "selected": T_j_set,
                    },
                },
            )
        except Exception:
            self._diagnostic_snapshot = None
            self._diagnostic_capture_failed = True

    @staticmethod
    def _model_parameter_names() -> set[str]:
        # These are members of the MATLAB params struct, not scalar workspace
        # symbols. Some source scalars (notably rho_solute) also have an
        # independently assigned derived member in that struct.
        return {key.removeprefix("params.") for key in parameter_defaults()["controller"]
                if key.startswith("params.")} | {
                    "rho_solute", "tau_1", "tau_2", "k_0", "k_0_proc",
                    "K_P_target", "K_I_target", "K_P_T", "K_I_T", "dT", "dc"}

    def _sync_parameter_view(self) -> None:
        self.params = {**(copy.deepcopy(self._model_params) if isinstance(self._model_params, Mapping) else {}),
                       **copy.deepcopy(self._root_workspace)}
        if not isinstance(self._model_params, Mapping):
            self.params["params"] = copy.deepcopy(self._model_params)
        # Public canonical names cannot express both roots. Computation uses
        # the distinct struct/workspace above; expose each known model member
        # by its established canonical name without letting an arbitrary
        # params.dt shadow the actual scalar dt.
        for name in self._model_parameter_names() - {"rho_solute"}:
            if isinstance(self._model_params, Mapping) and name in self._model_params:
                self.params[name] = copy.deepcopy(self._model_params[name])
        self.control_T_j_reference_K = self._root_workspace.get("T_j")

    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        """Assign the supplied GUI map; a missing variable stays undefined.

        Default generation belongs to the GUI/default-template helper. This
        endpoint is receive_modified/map2base and never silently fills it in.
        """
        if not isinstance(params, Mapping):
            raise TypeError("Controller parameters must be a mapping.")
        self.run_id = str(run_id)
        # This is the actual source Map, not a canonical-default template.
        # map2base assignments remain visible even if a later path fails.
        self.apply_modified(params)
        self._configured_params = copy.deepcopy(self.params)
        try:
            self.params_digest = parameter_digest(self._configured_params)
        except Exception:
            # A JSON archive identity is observational. Its serializer cannot
            # turn successful map2base assignment into an unacknowledged Map.
            self.params_digest = None
        self.configured = True
        self.running = False
        self.G_active = self._root_workspace.get("G_active", MATLAB_UNDEFINED)
        self.adaptation_enabled = self._root_workspace.get("adaptive", MATLAB_UNDEFINED)
        self.adaptation_mode = self._root_workspace.get("adaptive_mode", MATLAB_UNDEFINED)

    def start(self) -> None:
        """initialize_lists/ii=0; existing workspace objects survive reactivation."""
        if not self.configured:
            raise RuntimeError("configure() must be called before start().")
        self.frame_index = 0
        self.elapsed_s = 0.0
        self.history = {key: [] for key in HISTORY_KEYS}
        self._history_assigned = {key: [] for key in HISTORY_KEYS}
        self._history_tick_seq = []
        self._read_failures = []
        for key, values in self.history.items():
            setattr(self, f"{key}_list", values)
        self._activation_read_failure = None
        self._diagnostic_snapshot = None
        self._diagnostic_tick_data = None
        self._seed_generation_completed = False
        self.running = True
        if self.params["run_type"] == "simulation":
            for name, source in (("T", "T_init"), ("T_j", "T_j_init"),
                                 ("T_j_set", "T_j_set_init"), ("c", "c_init")):
                self.assign_initial_scalar(name, self.params[source], raw_units=False)
            self.simulation_state = {key: self._root_workspace[key]
                                     for key in ("T", "T_j", "T_j_set", "c")}

    def assign_initial_scalar(self, name: str, value: Any, *, raw_units: bool = True) -> None:
        if name not in {"T", "T_j", "T_j_set", "c", "count_middle"}:
            raise KeyError(name)
        if value is MATLAB_UNDEFINED:
            raise UnboundLocalError(f"Unrecognized function or variable '{name}'.")
        if is_matlab_empty(value):
            value = MATLAB_EMPTY
        elif raw_units and name in {"T", "T_j", "T_j_set"}:
            value = value + self.params["C2K"]
        self._root_workspace[name] = copy.deepcopy(value)
        if name == "T_j":
            self.control_T_j_reference_K = value
        self._sync_parameter_view()

    def complete_initialization(self) -> None:
        self._initialize_seed_population()

    def _initialize_seed_population(self) -> None:
        # initialize_controller performs rng(123) even in experiment mode.
        self.rng = MATLABRandom(123)
        self.size_list_seed = create_size_list(
            self._model_params, self.params["m_seed"], self.params["d_mean_seed"],
            self.params["d_std_seed"], rng=self.rng)
        self.size_list = 0 * self.size_list_seed
        self._seed_population["initial_count"] = len(self.size_list_seed)
        self._seed_generation_completed = True

    def initialize_process_state(self, process_state: ProcessState) -> None:
        for name in ("T", "T_j", "T_j_set", "c", "count_middle"):
            self.assign_initial_scalar(name, getattr(process_state, name), raw_units=False)
        self.complete_initialization()

    def record_activation_read_failure(self, error: Exception) -> None:
        partial = getattr(error, "partial_assignments", None)
        if partial:
            for assignment in partial:
                name, value = ((assignment["name"], assignment["value"])
                               if isinstance(assignment, Mapping) else assignment)
                self.assign_initial_scalar(name, value, raw_units=False)
        else:
            for name, value in getattr(error, "partial_process_values", {}).items():
                self.assign_initial_scalar(name, value, raw_units=False)
        self._activation_read_failure = {"pending": False, "read_failed": True,
            "error": str(error), "partial_process_values": copy.deepcopy(
                dict(getattr(error, "partial_process_values", {})))}

    def request_refresh(self) -> None:
        self.controller_refresh_request = True
        self._root_workspace["controller_refresh_request"] = True

    def refresh_pending(self) -> bool:
        return self._source_truth(self.controller_refresh_request)



    def prepare_iteration(self) -> None:
        """refresh_controller runs before record_controller_time (ii++)."""
        if not self._source_truth(self.controller_refresh_request):
            return
        self._initialize_filters(self._root_workspace["T"], self._root_workspace["c"])
        self.int_e_target_dt = 0.0
        self.int_e_dT_dt_dt = 0.0
        self.int_e_T_dt = 0.0
        self.controller_refresh_request = False
        self._root_workspace["controller_refresh_request"] = False
        self._sync_parameter_view()
        self.refresh_count += 1
        self.last_refresh_event = {"event_id": f"refresh-{self.refresh_count}",
            "effective_tick": self.frame_index + 1, "elapsed_s": self.elapsed_s,
            "reason": "explicit_refresh", "changes": {}}
        self._pending_refresh_initial_snapshot = {
            name: {"state": filter_.state.tolist(), "covariance": filter_.covariance.tolist()}
            for name, filter_ in self._diagnostic_filters().items() if filter_ is not None}

    def _bind_main_filter(self, params: Mapping[str, Any], dt: float) -> None:
        bound = copy.deepcopy(dict(params))
        self._filter_bindings = {"params": bound, "dt": dt}
        self.ekf.transition = lambda value, p=bound, step=dt: state_transition_function(p, value, step)
        self.ekf.measurement = lambda value, p=bound: measurement_function(p, value)

    def _initialize_filters(self, T: Any, c: Any) -> None:
        dt = self.params["dt"]
        state = ([] if is_matlab_empty(T) else [T]) + [0.] + ([] if is_matlab_empty(c) else [c]) + [0.]
        self.ekf = construct_EKF(np.asarray(state), self._model_params, dt)
        self._bind_main_filter(self._model_params, dt)
        self._filter_dts["main"] = dt
        for name in ("G_measure", "n", "k_0", "E_A"):
            setattr(self, f"ekf_{name}", create_EKF_general(
                dt, self.params[f"q2_{name}"], self.params[f"r_diag_{name}"], np.zeros(3)))
            self._filter_dts[name] = dt
        # refresh_adaptive clears only the logical selection vector.
        self.history["to_adapt"].clear()
        self._history_assigned["to_adapt"].clear()
        self._capture_initial_diagnostics()

    def set_g_active(self, active: bool) -> None:
        self.G_active = active
        self._root_workspace["G_active"] = active
        self._sync_parameter_view()

    def workspace_snapshot(self) -> Mapping[str, Any]:
        return matlab_value_document({**copy.deepcopy(self._root_workspace),
            "params": copy.deepcopy(self._model_params), "ii": self.frame_index,
            "controller_refresh_request": self.controller_refresh_request})


    def _noise_value(self, seed: int, scale: float, *, reseed: bool = True) -> float:
        if reseed:
            self.rng = MATLABRandom(seed)
        return float(self.rng.standard_normal() * scale)

    def _put(self, key: str, value: Any, index: int | None = None) -> None:
        """MATLAB's A(ii)=value, including indexed numeric zero expansion."""
        ii = self.frame_index if index is None else index
        if ii < 1:
            raise IndexError("Array indices must be positive integers or logical values.")
        values = self.history[key]
        assigned = self._history_assigned[key]
        if value is MATLAB_UNDEFINED:
            raise UnboundLocalError("Output argument was not assigned a value.")
        if is_matlab_empty(value):
            # Empty values returned by read_value/Map expressions are not
            # the source syntax A(ii)=[] used to delete an array element.
            if not values:
                error = ValueError("Unable to perform assignment because the indices on the left side are not compatible with the size of the right side.")
                error.reference_identifier = "MATLAB:subsassigndimmismatch"
            else:
                error = ValueError("Unable to perform assignment because the left and right sides have a different number of elements.")
                error.reference_identifier = "MATLAB:matrix:singleSubscriptNumelMismatch"
            raise error
        if isinstance(value, (list, tuple, np.ndarray)):
            if np.asarray(value).size != 1:
                raise ValueError("Unable to perform assignment because the left and right sides have a different number of elements.")
            value = np.asarray(value).reshape(-1)[0]
        if isinstance(value, str):
            if len(value) != 1:
                raise ValueError("Unable to perform assignment because the left and right sides have a different number of elements.")
            value = ord(value)
        while len(values) < ii:
            values.append(False if key == "to_adapt" else 0.0)
            assigned.append(False)
        values[ii - 1] = bool(value) if key == "to_adapt" else value
        assigned[ii - 1] = True

    def _at(self, key: str, index: int | None = None) -> Any:
        ii = self.frame_index if index is None else index
        if ii < 1 or ii > len(self.history[key]):
            raise IndexError("Index exceeds the number of array elements.")
        return self.history[key][ii - 1]

    def _measure(self, tick: ControllerTickInput) -> None:
        dt = tick.controller_dt_s
        if self.params["run_type"] == "simulation":
            self._put("t", dt if self.frame_index == 1 else self._at("t", self.frame_index - 1) + dt)
        else:
            self._put("t", tick.elapsed_s)
        self.elapsed_s = self._at("t")
        self._history_tick_seq = list(range(1, len(self.history["t"]) + 1))
        if self.pending_seed:
            if self.params["run_type"] == "simulation":
                # Repeated add_seed_request replaces the current array.
                self.size_list = self.size_list_seed.copy()
                self._seed_population["additions"].append({"tick_seq": self.frame_index,
                    "added_count": len(self.size_list_seed), "total_count": len(self.size_list)})
            self.pending_seed = False
            self.mark_seed = True
            self._root_workspace["add_seed_request"] = False
            self._root_workspace["mark_seed"] = True
            self._sync_parameter_view()
        if self.params["run_type"] == "simulation":
            if self.frame_index == 1:
                for name in ("T", "T_j", "c"):
                    self._put(name, self._root_workspace[name])
            else:
                previous = self.frame_index - 1
                self._put("T", state_transition_function_T(self._model_params,
                    self._at("T", previous), self._at("T_j", previous), dt))
                self._put("T_j", update_T_j(self._at("T_j_set", previous), self._at("T_j", previous),
                    self.params["dT_j_dt_max"], dt, self.params["dT_j"], self.params["dt_update"],
                    self.params["T_j_min"], self.params["T_j_max"]))
                next_c, next_size = calc_next_crystallization_mass_balance(self._model_params,
                    self.params["m_solvent"] * self._at("c", previous), self.params["m_solvent"],
                    self.size_list, self._at("T", previous), dt)
                self._put("c", next_c)
                self.size_list = next_size
                self._put("T", self._at("T") + self._noise_value(self.frame_index * 456, .01))
                self._put("c", self._at("c") + self._noise_value(self.frame_index * 789, .0002))
            self.simulation_state = {name: self._at(name) for name in ("T", "T_j", "c")}
            return
        for name in ("T", "T_j", "c", "count_middle"):
            if tick.process_reader is not None:
                value = tick.process_reader(name)
                if name in {"T", "T_j"} and not is_matlab_empty(value) and value is not MATLAB_UNDEFINED:
                    value = value + self.params["C2K"]
            else:
                if tick.process_state is None:
                    raise RuntimeError("Experiment tick is missing its process read.")
                value = getattr(tick.process_state, name)
            if name in {"c", "count_middle"} and is_matlab_empty(value):
                # safe_read_value falls back only after a successful EMPTY
                # read. A communication exception never reaches this branch.
                value = self.history[name][-1]
            self._put(name, value)

    def _growth_values(
        self, tick: ControllerTickInput, c_KF: float, T_KF: float
    ) -> tuple[tuple[float | None, float | None, float | None, float | None] | None, bool, str | None]:
        if self.params["run_type"] == "simulation" and self.params["growth_rate_source"] == "simulated":
            # read_growth_rate resets this stream before calling calc_G, so
            # the new RNG state also survives an exception inside that call.
            self.rng = MATLABRandom(self.frame_index * 1122)
            model = calc_G(self._model_params, c_KF, T_KF, is_model=True)
            seeds = (1122, 3344, 5566, 7788)
            values = tuple(
                model + self._noise_value(self.frame_index * seed, 1e-9,
                    reseed=offset != 0)
                for offset, seed in enumerate(seeds)
            )
            return values, True, None
        sample = tick.growth_sample
        if sample is None:
            return None, False, "no_growth_sample"
        if isinstance(sample, Mapping):
            # A successful MATLAB G-map read can lack an individual key. This
            # local read value is distinct from the stricter AMQP sample schema.
            optional_values = tuple(sample.get(key) for key in ("G_u", "G_u_KF", "G_v", "G_v_KF"))
        else:
            # valid describes the producer's observation; it is not part of
            # MATLAB's map read and must not replace supplied numeric fields.
            optional_values = (sample.G_u, sample.G_u_KF, sample.G_v, sample.G_v_KF)
        return optional_values, True, None

    def record_read_failure(self, tick: ControllerTickInput, error: Exception | None = None,
                            *, stage: str = "process_read") -> None:
        if any(item["tick_seq"] == tick.tick_seq for item in self._read_failures):
            return
        self._read_failures.append({"tick_seq": tick.tick_seq, "elapsed_s": self.elapsed_s,
            "stage": getattr(error, "stage", stage), "message": str(error) if error else ""})
        self._diagnostic_snapshot = None

    def refresh_status(self) -> dict[str, Any]:
        return {
            "count": self.refresh_count, "segment_id": self.segment_id,
            "last_event": copy.deepcopy(self.last_refresh_event),
        }



    def step(self, tick_input: ControllerTickInput) -> ControllerStepResult | None:
        if not self.running:
            raise RuntimeError("CrystallizationController is not running.")
        if not isinstance(tick_input, ControllerTickInput):
            raise TypeError("step() requires ControllerTickInput.")
        if tick_input.tick_seq != self.frame_index + 1:
            raise ValueError("Controller tick sequence must be contiguous.")
        self.prepare_iteration()
        self.frame_index = tick_input.tick_seq
        self.elapsed_s = tick_input.elapsed_s
        self._diagnostic_snapshot = None
        self._diagnostic_capture_failed = False
        self._diagnostic_tick_data = {"filters": {}, "refresh_init": None, "control": {}}
        if self._pending_refresh_initial_snapshot is not None:
            self._diagnostic_tick_data["refresh_init"] = ControllerDiagnosticsSnapshot.from_data(
                tick_seq=self.frame_index, elapsed_s=self.elapsed_s, run_id=str(self.run_id),
                fields={"diag_snapshot_stage": "refresh_init"},
                raw={"refresh_init": copy.deepcopy(self._pending_refresh_initial_snapshot)})
            self._pending_refresh_initial_snapshot = None
        self._attach_diagnostic_observers()
        dt = tick_input.controller_dt_s
        stage = "process_read"
        candidate_result = None
        G_measure = G_measure_KF = None
        try:
            self._measure(tick_input)
            T, T_j, c = (self._at(name) for name in ("T", "T_j", "c"))
            stage = "measurement_derivative"
            dT_dt = (T - self._at("T", self.frame_index - 1)) / dt if self.frame_index > 1 else 0.0
            self._put("dT_dt", dT_dt)
            dc_dt = (c - self._at("c", self.frame_index - 1)) / dt if self.frame_index > 1 else 0.0
            self._put("dc_dt", dc_dt)
            stage = "main_filter"
            if self.ekf is None:
                raise ControllerInputValueError("Unrecognized function or variable 'EKF'.",
                    stage="main_filter", reference_identifier="MATLAB:UndefinedFunction")
            filtered = smooth_EKF(self.ekf, self._model_params, np.array([T, dT_dt, c, dc_dt]))
            T_KF, dT_dt_KF, c_KF, dc_dt_KF = filtered
            for name, value in zip(("T_KF", "dT_dt_KF", "c_KF", "dc_dt_KF"), filtered):
                self._put(name, value)
            stage = "target"
            sigma = calc_sigma(c_KF, T_KF)[0]
            self._put("sigma", sigma)
            G_model = calc_G(self._model_params, c_KF, T_KF)
            self._put("G_model", G_model)
            target = self.params["target"]
            if target == "sigma":
                self._put("target", sigma)
            elif target == "G":
                self._put("target", G_model)
            target_value = self._at("target")
            self._fill_target("target", target)
            target_set = self.params["target_set"]
            self._put("target_set", target_set)
            self._fill_target("target_set", target)
            absolute_error = abs(target_value - target_set)
            self._put("abs_e_target", absolute_error)
            self._fill_target("abs_e_target", target)
            stage = "lag"
            lag_percentage = calc_t_lag_perc(self.params["t_lag_perc"],
                self.params["t_lag_threshold_perc"], target_set, absolute_error)
            self._put("t_lag_perc", lag_percentage)
            lag = self.params["t_lag"] * lag_percentage
            self._put("t_lag", lag)
            projected = filtered + np.array([filtered[1], 0.0, filtered[3], 0.0]) * lag
            stage = "control"
            dT_dt_request = None
            if abs(calc_relative_sigma(projected[2], projected[0])) < self.params["sigma_threshold"]:
                self._put("dT_dt_set", self._at("dT_dt_set", self.frame_index - 1))
                self._put("int_e_target_dt", self._at("int_e_target_dt", self.frame_index - 1))
                self._put("T_j_set", self._at("T_j_set", self.frame_index - 1))
            else:
                request, integral = calc_dT_dt_set(self._model_params, projected,
                    self.params["mode"], target, target_set, self.params["dT_dt_min"],
                    self.params["dT_dt_max"], dt, self.int_e_target_dt)
                # MATLAB multi-output assignment happens only after return.
                self._put("dT_dt_set", request)
                self.int_e_target_dt = integral
                self._put("int_e_target_dt", integral)
                dT_dt_request = request
                command, rate_integral, temperature_integral = calc_T_j_set(
                    self._model_params, projected, request, self.int_e_dT_dt_dt, self.int_e_T_dt,
                    self._root_workspace["T_j"], self.params["T_j_min"], self.params["T_j_max"],
                    dt, target == "G", observer=self._capture_control_diagnostics)
                self._put("T_j_set", command)
                self.int_e_dT_dt_dt = rate_integral
                self.int_e_T_dt = temperature_integral
            dT_dt_set, T_j_set = self._at("dT_dt_set"), self._at("T_j_set")
            objective = objective_function(self._model_params, filtered, target, dT_dt_set, target_set, dt)
            self._put("objective", objective)
            self._fill_target("objective", target)
            values = dict(T=T, T_j=T_j, c=c, dT_dt=dT_dt, dc_dt=dc_dt,
                T_KF=T_KF, dT_dt_KF=dT_dt_KF, c_KF=c_KF, dc_dt_KF=dc_dt_KF,
                sigma=sigma, G_model=G_model, G_measure=None, G_measure_KF=None,
                target_value=target_value, target_set=target_set, target_error_abs=absolute_error,
                dT_dt_set=dT_dt_set, T_j_set=T_j_set, objective=objective,
                E_A=self._model_params["E_A"], k_0=self._model_params["k_0"], n=self._model_params["n"],
                t_lag=lag, t_lag_perc=lag_percentage)
            candidate_result = ControllerStepResult(**values)
            stage = "growth_read"
            active = self._source_flag("G_active") if tick_input.growth_active is None else tick_input.growth_active
            should_adapt = False
            if active:
                stage = "growth_read"
                if tick_input.growth_reader is not None:
                    try:
                        sample = tick_input.growth_reader()
                    except (TimeoutError, ConnectionError) as exc:
                        raise ControllerReadError(str(exc), stage="growth_read") from exc
                    tick_input = replace(tick_input, growth_sample=sample, growth_reader=None,
                                         growth_sample_age_s=None)
                growth_values, _, _ = self._growth_values(tick_input, c_KF, T_KF)
                # read_from_gsensor_to_controller owns four local arrays;
                # each bad/missing field becomes missing independently.
                for name, value in zip(("G_u", "G_u_KF", "G_v", "G_v_KF"),
                                       growth_values or (None,) * 4):
                    try:
                        self._put(name, float("nan") if value is None else value)
                    except Exception:
                        self._put(name, float("nan"))
                should_adapt = self._source_flag("adaptive")
                if should_adapt:
                    stage = "growth_filter"
                    display_flag = self._source_scalar("adaptive_disp")
                    # Source tests if ~adaptive_disp, i.e. every element of
                    # the elementwise negation, rather than !(all elements).
                    if self._source_truth(source_not(display_flag)):
                        self._root_workspace["adaptive_disp"] = True
                    G_measure = .5 * (self._at("G_u_KF") + self._at("G_v_KF"))
                    self._put("G_measure", G_measure)
                    G_measure_KF = smooth_EKF_general(self.history["G_measure"], self.ekf_G_measure, dt)
                    self._put("G_measure_KF", G_measure_KF)
                    self._put("to_adapt", True)
                    stage = "adaptation"
                    # Pass the full indexed source arrays. adapt_growth_parameters
                    # applies logical mask, removes missing/nonpositive values,
                    # and only THEN retains its last max_num_adapt samples.
                    selected = list(self.history["to_adapt"])
                    length = len(selected)
                    new_params, new_count = adapt_growth_parameters(self._model_params,
                        self.history["G_measure_KF"][:length], self.history["sigma"][:length],
                        self.history["T"][:length], selected, self.params["max_num_adapt"],
                        self.params["min_num_adapt"], self._source_scalar("adaptive_mode"))
                    self._model_params = new_params
                    self.num_adapt = new_count
                    self._sync_parameter_view()
                    fitted = not (new_count < self.params["min_num_adapt"])
                    if fitted:
                        self._bind_main_filter(self._model_params, dt)
                        self.adaptation_diagnostics["fit_count"] += 1
                    self.adaptation_diagnostics.update(last_status="fit_succeeded" if fitted else "waiting_for_samples",
                        last_tick=self.frame_index, last_mode=self.adaptation_mode)
                else:
                    # controller_for_gui checks this workspace flag before
                    # clearing it. An undefined flag is a source exception,
                    # even though the optional console message has no state.
                    self._source_flag("adaptive_disp")
                    self._root_workspace["adaptive_disp"] = False
            stage = "record_parameters"
            for name in ("n", "n_KF", "k_0", "k_0_KF", "E_A", "E_A_KF"):
                self._put(name, self._model_params[name.removesuffix("_KF")])
            self.fitting_G_measure_history = list(self.history["G_measure"])
            self.fitting_last_tick = self.frame_index if should_adapt else self.fitting_last_tick
            self.fitting_segment = {"G": list(self.history["G_measure_KF"]),
                "sigma": list(self.history["sigma"]), "T": list(self.history["T"]),
                "ticks": list(range(1, len(self.history["G_measure_KF"]) + 1))}
            self.last_control_hold_reason = None
            self.last_adaptation_pause_reason = None
            self._finish_diagnostics(T=T, c=c, T_j=T_j, G_measure=G_measure,
                refreshed=bool(self.last_refresh_event and self.last_refresh_event["effective_tick"] == self.frame_index),
                growth_updated=should_adapt, growth_reanchored=False, projected=projected,
                dT_dt_request=dT_dt_request, T_j_set=T_j_set)
            return replace(candidate_result, G_measure=G_measure, G_measure_KF=G_measure_KF,
                E_A=self._model_params["E_A"], k_0=self._model_params["k_0"], n=self._model_params["n"])
        except Exception as exc:
            # One source-loop warning: preserve every completed assignment,
            # filter mutation and integral. Do not manufacture a normal point
            # or undo receive_modified because a later source stage failed.
            if isinstance(exc, (ControllerReadError, ControllerInputValueError)):
                failure = exc
                if isinstance(failure, ControllerReadError):
                    failure.stage = stage
            else:
                failure = ControllerInputValueError(str(exc), stage=stage,
                    reference_identifier=getattr(exc, "reference_identifier", None))
            failure.partial_result = candidate_result
            if stage == "adaptation":
                self.adaptation_diagnostics["failure_count"] += 1
                self.adaptation_diagnostics.update(last_status="fit_failed", last_tick=self.frame_index,
                    last_mode=self.adaptation_mode, last_failure_reason=str(exc))
            self.record_read_failure(tick_input, failure, stage=stage)
            if failure is exc:
                raise
            raise failure from exc

    def _fill_target(self, source: str, target: str) -> None:
        names = {"target": ("target_sigma", "target_G"),
                 "target_set": ("target_sigma_set", "target_G_set"),
                 "abs_e_target": ("abs_e_target_sigma", "abs_e_target_G"),
                 "objective": ("objective_sigma", "objective_G")}[source]
        for mode, name in zip(("sigma", "G"), names):
            self._put(name, self._at(source) if mode == target else float("nan"))

    def _source_scalar(self, name: str) -> Any:
        if name not in self._root_workspace:
            raise ControllerInputValueError(f"Unrecognized function or variable '{name}'.",
                stage="growth_read" if name in {"G_active", "adaptive"} else "adaptation",
                reference_identifier="MATLAB:UndefinedFunction")
        return self._root_workspace[name]

    def _source_flag(self, name: str) -> bool:
        value = self._source_scalar(name)
        return self._source_truth(value)

    @staticmethod
    def _source_truth(value: Any) -> bool:
        return source_if(value)

    def stop(self) -> None:
        self.running = False


    def runtime_configuration(self) -> dict[str, Any]:
        return {
            "control_target": self.params["target"],
            "sigma_set": (
                self.params["target_set"] if self.params["target"] == "sigma"
                else self.params["sigma_set"]
            ),
            "G_set": (
                self.params["target_set"] if self.params["target"] == "G"
                else self.params["G_set"]
            ),
            "adaptation_enabled": self.adaptation_enabled,
            "adaptation_mode": self.adaptation_mode,
        }

    def adaptation_status(self) -> dict[str, Any]:
        # A failed optimizer call does not return num_adapt. Report the
        # currently eligible window, rather than the last returned fit count.
        eligible_samples = self.num_adapt
        return {
            **copy.deepcopy(self.adaptation_diagnostics),
            "enabled": self.adaptation_enabled, "mode": self.adaptation_mode,
            "sample_count": eligible_samples,
            "last_fit_sample_count": self.num_adapt,
            "minimum_samples": self.params["min_num_adapt"],
            "pause_reason": self.last_adaptation_pause_reason,
            "control_hold_reason": self.last_control_hold_reason,
            "segment_id": self.segment_id,
            "segment_sample_count": eligible_samples,
            "segment_start_tick": self.segment_start_tick,
        }

    def apply_modified(self, mapping: Mapping[str, Any]) -> Mapping[str, Any]:
        """map2base: sorted paths, incremental assignment, no derived recompute."""
        if not isinstance(mapping, Mapping):
            raise TypeError("Modified values must be a mapping.")
        event_id = mapping.get("event_id")
        modified = dict(mapping)
        aliases = {"control_target": "target", "exp_sim": "run_type",
                   "exp_sim_G": "growth_rate_source", "adaptation_enabled": "adaptive",
                   "adaptation_mode": "adaptive_mode"}
        paths = dict(modified)
        assignments = []
        for key in sorted(paths):
            value = paths[key]
            if isinstance(value, Mapping):
                assignments.extend((f"{key}.{name}", value[name]) for name in sorted(value))
            else:
                assignments.append((key, value))
        for original, raw_value in assignments:
            value = copy.deepcopy(raw_value)
            key = aliases.get(original, original)
            import re
            if any(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", part) is None for part in key.split(".")):
                raise ValueError(f"Invalid MATLAB workspace variable {key!r}.")
            if key == "params":
                self._model_params = value
            elif key.startswith("params."):
                members = key[7:].split(".")
                destination = self._model_params
                for member in members[:-1]:
                    if member not in destination:
                        destination[member] = {}
                    destination = destination[member]
                destination[members[-1]] = value
            else:
                if key == "growth_rate_source":
                    value = {"simulation": "simulated", "experiment": "live_gsensor",
                             "experiment_with_presaved": "presaved_images"}.get(value, value)
                parts = key.split(".")
                destination = self._root_workspace
                for part in parts[:-1]:
                    if part not in destination:
                        destination[part] = {}
                    destination = destination[part]
                destination[parts[-1]] = value
                if key == "adaptive":
                    self.adaptation_enabled = value
                elif key == "adaptive_mode":
                    self.adaptation_mode = value
                elif key == "G_active":
                    self.G_active = value
                elif key == "controller_refresh_request":
                    self.controller_refresh_request = value
                elif key == "add_seed_request":
                    self.pending_seed = value
                elif key == "mark_seed":
                    self.mark_seed = value
                elif key in {"num_adapt", "int_e_target_dt", "int_e_dT_dt_dt", "int_e_T_dt"}:
                    setattr(self, key, value)
            # Each preceding source assignment remains visible if a later
            # nested path assignment fails.
            self._sync_parameter_view()
        receipt = {"event_id": event_id, "effective_tick": self.frame_index + 1,
            "elapsed_s": self.elapsed_s, "refreshed": False,
            "changes": copy.deepcopy(mapping.get("changes", {})),
            "parameter_changes": copy.deepcopy(mapping.get("parameter_changes", {})),
            "assigned": copy.deepcopy(paths)}
        self._last_parameter_event = receipt
        if event_id is not None:
            self._runtime_parameter_ledger.append(copy.deepcopy(receipt))
        return copy.deepcopy(receipt)

    def validate_runtime_update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, Mapping):
            raise TypeError("Runtime changes must be a mapping.")
        return dict(changes)

    def runtime_parameter_values(self) -> dict[str, dict[str, Any]]:
        return {section: {key: copy.deepcopy(
                    self._model_params[key[7:]] if key.startswith("params.") else self._root_workspace[key])
                for key in values if (key[7:] in self._model_params if key.startswith("params.")
                                      else key in self._root_workspace)}
                for section, values in parameter_defaults().items() if section != "gsensor"}

    def validate_parameter_update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, Mapping):
            raise TypeError("Parameter changes must be a mapping.")
        return copy.deepcopy(dict(changes))

    def runtime_parameter_status(self) -> dict[str, Any]:
        return {"last_event": copy.deepcopy(self._last_parameter_event),
                "applied_event_ids": [item["event_id"] for item in self._runtime_parameter_ledger],
                "derived_values": {key: copy.deepcopy(self.params[key])
                                   for key in derived_parameter_policies() if key in self.params}}

    def update_runtime(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        self.apply_modified(changes)
        return self.runtime_configuration()

    def set_adaptation(
        self, enabled: bool, mode: str, event: Mapping[str, Any] | None = None
    ) -> None:
        del event
        modes = {"E_A", "k_0", "n", "E_A_and_k_0", "E_A_and_n", "k_0_and_n", "all"}
        if not isinstance(enabled, bool):
            raise TypeError("adaptation enabled must be bool.")
        if mode not in modes:
            raise ValueError("Unsupported adaptation mode.")
        self.adaptation_enabled = enabled
        self.adaptation_mode = mode
        self._root_workspace["adaptive"] = enabled
        self._root_workspace["adaptive_mode"] = mode
        self._sync_parameter_view()

    def export_state(self) -> Mapping[str, Any] | None:
        """Archive actual workspace state, including an interrupted prefix.

        Archives are observational; restoring one does not activate equipment
        or authorize the scheduler to resume a process after service restart.
        """
        if not self.configured:
            return None
        filters = {}
        for name, filter_ in self._diagnostic_filters().items():
            if filter_ is None:
                filters[name] = None
                continue
            filters[name] = {"state": filter_.state.copy(),
                "covariance": filter_.covariance.copy(),
                "sqrt_covariance": copy.deepcopy(filter_._sqrt_covariance),
                "process_noise": filter_.process_noise.copy(),
                "measurement_noise": filter_.measurement_noise.copy(),
                "initial_covariance": copy.deepcopy(filter_._diagnostic_initial_covariance),
                "dt": self._filter_dts["G_measure" if name == "G" else name]}
        return _json_value(matlab_value_document({
            "schema": "matlab_gui_workspace_v1", "baseline_commit": BASELINE_COMMIT,
            "run_id": self.run_id, "params_digest": self.params_digest,
            "configured_params": self._configured_params, "running": self.running,
            "frame_index": self.frame_index, "elapsed_s": self.elapsed_s,
            "workspace": self._root_workspace, "model_params": self._model_params,
            "params": self.params, "history": self.history,
            "history_assigned": self._history_assigned,
            "history_tick_seq": self._history_tick_seq,
            "read_failures": self._read_failures,
            "runtime_parameter_updates": self._runtime_parameter_ledger,
            "integrals": {"int_e_target_dt": self.int_e_target_dt,
                "int_e_dT_dt_dt": self.int_e_dT_dt_dt, "int_e_T_dt": self.int_e_T_dt},
            "simulation": {"state": self.simulation_state,
                "size_list_seed": self.size_list_seed, "size_list": self.size_list,
                "pending_seed": self.pending_seed, "mark_seed": self.mark_seed,
                "seed_generation_completed": self._seed_generation_completed,
                "seed_population": self._seed_population, "rng_state": self.rng.bit_generator.state},
            "filters": filters, "filter_bindings": self._filter_bindings,
            "refresh": {"pending": self.controller_refresh_request,
                "count": self.refresh_count, "last_event": self.last_refresh_event},
            "initialization": self._activation_read_failure,
            "adaptation": {"enabled": self.adaptation_enabled, "mode": self.adaptation_mode,
                "G_active": self.G_active, "num_adapt": self.num_adapt,
                "diagnostics": self.adaptation_diagnostics}}))

    def restore_state(self, params: Mapping[str, Any], run_id: str, state: Mapping[str, Any]) -> bool:
        """Restore the exact archived numerical object without reading devices."""
        self.running = False
        try:
            state = decode_nonfinite(copy.deepcopy(dict(state)))
            validate_checkpoint(state, params, HISTORY_KEYS)
            if state["run_id"] != run_id:
                return False
            def untag(value):
                if isinstance(value, Mapping):
                    if value == {"__matlab_value__": "empty"}:
                        return MATLAB_EMPTY
                    if value == {"__matlab_value__": "empty_cell"}:
                        return MATLAB_EMPTY_CELL
                    if value == {"__matlab_value__": "undefined"}:
                        return MATLAB_UNDEFINED
                    return {key: untag(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [untag(item) for item in value]
                return value
            state = untag(state)
            self.run_id = run_id
            self.configured = True
            self.params_digest = state["params_digest"]
            self._configured_params = copy.deepcopy(state["configured_params"])
            self._root_workspace = copy.deepcopy(state["workspace"])
            self._model_params = copy.deepcopy(state["model_params"])
            self._sync_parameter_view()
            self.control_T_j_reference_K = self._root_workspace.get("T_j")
            self.frame_index = state["frame_index"]
            self.elapsed_s = state["elapsed_s"]
            self.history = copy.deepcopy(state["history"])
            self._history_assigned = copy.deepcopy(state["history_assigned"])
            self._history_tick_seq = list(state["history_tick_seq"])
            self._read_failures = copy.deepcopy(state["read_failures"])
            for key, values in self.history.items():
                setattr(self, f"{key}_list", values)
            for name, value in state["integrals"].items():
                setattr(self, name, value)
            self._runtime_parameter_ledger = copy.deepcopy(state["runtime_parameter_updates"])
            self._last_parameter_event = (copy.deepcopy(self._runtime_parameter_ledger[-1])
                                          if self._runtime_parameter_ledger else None)
            simulation = state["simulation"]
            self.simulation_state = copy.deepcopy(simulation["state"])
            self.size_list_seed = np.asarray(simulation["size_list_seed"])
            self.size_list = np.asarray(simulation["size_list"])
            self.pending_seed = simulation["pending_seed"]
            self.mark_seed = simulation["mark_seed"]
            self._seed_generation_completed = simulation["seed_generation_completed"]
            self._seed_population = copy.deepcopy(simulation["seed_population"])
            self.rng = MATLABRandom(0)
            self.rng.bit_generator.state = simulation["rng_state"]
            self._filter_bindings = copy.deepcopy(state["filter_bindings"])
            self._filter_dts = {}
            for name, saved in state["filters"].items():
                attribute = "ekf" if name == "main" else f"ekf_{name if name != 'G' else 'G_measure'}"
                if saved is None:
                    setattr(self, attribute, None)
                    continue
                dim = len(saved["state"])
                dt = saved["dt"]
                self._filter_dts["G_measure" if name == "G" else name] = dt
                # Construct a neutral object, then restore its actual internal
                # state/Q/R/P. Failed predict/correct can leave nonfinite state;
                # re-running a validating State constructor would change it.
                F = np.array([[1, dt, dt * dt / 2], [0, 1, dt], [0, 0, 1]])
                filter_ = ExtendedKalmanFilter(lambda value, f=F: f @ value,
                    lambda value: value, np.zeros(dim), np.asarray(saved["process_noise"]),
                    np.asarray(saved["measurement_noise"]), np.eye(dim))
                restore_EKF(filter_, saved)
                filter_._diagnostic_initial_covariance = np.asarray(saved["initial_covariance"])
                setattr(self, attribute, filter_)
            if self.ekf is not None:
                self._bind_main_filter(self._filter_bindings["params"], self._filter_bindings["dt"])
            self.controller_refresh_request = state["refresh"]["pending"]
            self.refresh_count = state["refresh"]["count"]
            self.last_refresh_event = copy.deepcopy(state["refresh"]["last_event"])
            self._activation_read_failure = copy.deepcopy(state["initialization"])
            adaptation = state["adaptation"]
            self.adaptation_enabled = adaptation["enabled"]
            self.adaptation_mode = adaptation["mode"]
            self.G_active = adaptation["G_active"]
            self.num_adapt = adaptation["num_adapt"]
            self.adaptation_diagnostics = copy.deepcopy(adaptation["diagnostics"])
            self.running = state["running"]
            return True
        except (KeyError, TypeError, ValueError, OverflowError):
            self.running = False
            return False


__all__ = ["BASELINE_COMMIT", "CrystallizationController"]
