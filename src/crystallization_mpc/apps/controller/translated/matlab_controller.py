"""Stateful Python translation of the frozen MATLAB Controller loop."""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping

import numpy as np

from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.tick import ControllerTickInput

from .adaptation import AdaptationError, adapt_growth_parameters
from .control import (
    OptimizationError,
    calc_T_j_set,
    calc_dT_dt,
    calc_dT_dt_set,
    calc_dc_dt,
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
)
from .mass_balance import calc_next_crystallization_mass_balance, create_size_list
from .parameters import build_parameters, parameter_digest
from .thermodynamics import calc_G, calc_relative_sigma, calc_sigma


BASELINE_COMMIT = "ce885a13e0a3e95ac0509e06eebf9d7cd1d418b0"
ALGORITHM_STATE_SCHEMA_VERSION = 1

HISTORY_KEYS = (
    "t", "T", "T_KF", "T_j_set", "T_j", "dT_dt", "dT_dt_KF",
    "dT_dt_set", "c", "c_KF", "dc_dt", "dc_dt_KF", "target",
    "target_set", "abs_e_target", "objective", "target_sigma",
    "target_sigma_set", "abs_e_target_sigma", "objective_sigma", "target_G",
    "target_G_set", "abs_e_target_G", "objective_G", "sigma", "G_model",
    "G_u", "G_u_KF", "G_v", "G_v_KF", "G_measure", "G_measure_KF",
    "count_middle", "n", "k_0", "E_A", "t_lag", "t_lag_perc", "to_adapt",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return _json_value(value.tolist())
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


class MatlabController:
    """Own the MATLAB workspace variables for one configured experiment."""

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}
        self._configured_params: dict[str, Any] = {}
        self.params_digest: str | None = None
        self.run_id: str | None = None
        self.configured = False
        self.running = False
        self.adaptation_enabled = False
        self.adaptation_mode = "E_A"
        self._reset_runtime_state()

    def _reset_runtime_state(self) -> None:
        self.frame_index = 0
        self.elapsed_s = 0.0
        self.history: dict[str, list[Any]] = {key: [] for key in HISTORY_KEYS}
        for key, values in self.history.items():
            setattr(self, f"{key}_list", values)
        self.int_e_target_dt = 0.0
        self.int_e_dT_dt_dt = 0.0
        self.int_e_T_dt = 0.0
        self.num_adapt = 0
        self.pending_seed = False
        self.mark_seed = False
        self.size_list_seed = np.empty(0, dtype=float)
        self.size_list = np.empty(0, dtype=float)
        self.simulation_state: dict[str, float] | None = None
        self.ekf: ExtendedKalmanFilter | None = None
        self.ekf_G_measure: ExtendedKalmanFilter | None = None
        self.ekf_n: ExtendedKalmanFilter | None = None
        self.ekf_k_0: ExtendedKalmanFilter | None = None
        self.ekf_E_A: ExtendedKalmanFilter | None = None
        self.last_adaptation_pause_reason: str | None = None

    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        if not isinstance(params, Mapping):
            raise TypeError("Controller parameters must be a mapping.")
        normalized_run_id = str(run_id).strip()
        if not normalized_run_id:
            raise ValueError("Controller requires run_id.")
        normalized = build_parameters(params)
        self.params = copy.deepcopy(normalized)
        self._configured_params = copy.deepcopy(normalized)
        self.params_digest = parameter_digest(self._configured_params)
        self.run_id = normalized_run_id
        self.configured = True
        self.running = False
        self._reset_runtime_state()

    def start(self) -> None:
        if not self.configured:
            raise RuntimeError("configure() must be called before start().")
        self._reset_runtime_state()
        initial_T = float(self.params["T_init"])
        initial_c = float(self.params["c_init"])
        self.simulation_state = {
            "T": initial_T,
            "T_j": float(self.params["T_j_init"]),
            "T_j_set": float(self.params["T_j_set_init"]),
            "c": initial_c,
            "count_middle": 0.0,
        }
        self.size_list_seed = create_size_list(
            self.params,
            float(self.params["m_seed"]),
            float(self.params["d_mean_seed"]),
            float(self.params["d_std_seed"]),
            rng=np.random.default_rng(123),
        )
        self.size_list = np.zeros_like(self.size_list_seed)
        if self.params["run_type"] == "simulation":
            self._initialize_filters(initial_T, initial_c)
        self.running = True

    def _initialize_filters(self, T: float, c: float) -> None:
        dt = float(self.params["dt"])
        self.ekf = construct_EKF(np.array([T, 0.0, c, 0.0]), self.params, dt)
        self.ekf.transition = lambda value: state_transition_function(self.params, value, dt)
        self.ekf.measurement = lambda value: np.array([value[0], value[2]])
        self.ekf_G_measure = create_EKF_general(
            dt, float(self.params["q2_G_measure"]), self.params["r_diag_G_measure"], np.zeros(3)
        )
        self.ekf_n = create_EKF_general(
            dt, float(self.params["q2_n"]), self.params["r_diag_n"], np.zeros(3)
        )
        self.ekf_k_0 = create_EKF_general(
            dt, float(self.params["q2_k_0"]), self.params["r_diag_k_0"], np.zeros(3)
        )
        self.ekf_E_A = create_EKF_general(
            dt, float(self.params["q2_E_A"]), self.params["r_diag_E_A"], np.zeros(3)
        )

    def _noise_value(self, name: str, index: int, seed: int, scale: float) -> float:
        supplied = self.params.get("simulation_noise")
        if isinstance(supplied, Mapping):
            values = supplied.get(name)
            if isinstance(values, (list, tuple, np.ndarray)) and index < len(values):
                return float(values[index])
        # Exact R2021a arrays can be injected from the golden fixture.
        return float(np.random.RandomState(seed).standard_normal()) * scale

    def _advance_simulation(self, tick_index: int, dt: float) -> dict[str, float]:
        if self.simulation_state is None:
            raise RuntimeError("Simulation state is not initialized.")
        if tick_index == 0:
            return dict(self.simulation_state)
        previous = dict(self.simulation_state)
        if self.pending_seed:
            self.size_list = self.size_list_seed.copy()
            self.pending_seed = False
            self.mark_seed = True
        T = state_transition_function_T(self.params, previous["T"], previous["T_j"], dt)
        T_j = update_T_j(
            previous["T_j_set"], previous["T_j"], float(self.params["dT_j_dt_max"]),
            dt, float(self.params["dT_j"]), float(self.params["dt_update"]),
            float(self.params["T_j_min"]), float(self.params["T_j_max"]),
        )
        c, self.size_list = calc_next_crystallization_mass_balance(
            self.params, float(self.params["m_solvent"]) * previous["c"],
            float(self.params["m_solvent"]), self.size_list, previous["T"], dt,
        )
        matlab_index = tick_index + 1
        T += self._noise_value("T_noise", tick_index, matlab_index * 456, 0.01)
        c += self._noise_value("c_noise", tick_index, matlab_index * 789, 0.0002)
        self.simulation_state.update({"T": T, "T_j": T_j, "c": c})
        return dict(self.simulation_state)

    def _growth_values(
        self, tick: ControllerTickInput, c_KF: float, T_KF: float
    ) -> tuple[tuple[float, float, float, float] | None, bool, str | None]:
        if self.params["growth_rate_source"] == "simulated":
            model = float(calc_G(self.params, c_KF, T_KF, is_model=True))
            index = self.frame_index - 1
            seeds = (1122, 3344, 5566, 7788)
            names = ("G_u_noise", "G_u_KF_noise", "G_v_noise", "G_v_KF_noise")
            values = tuple(
                model + self._noise_value(name, index, self.frame_index * seed, 1e-9)
                for name, seed in zip(names, seeds)
            )
            return values, True, None
        sample = tick.growth_sample
        if sample is None:
            return None, False, "no_growth_sample"
        if not sample.valid:
            return None, False, "invalid_growth_sample"
        optional_values = (sample.G_u, sample.G_u_KF, sample.G_v, sample.G_v_KF)
        if any(value is None for value in optional_values):
            return None, False, "incomplete_growth_sample"
        values = tuple(float(value) for value in optional_values)  # type: ignore[arg-type]
        if (
            tick.growth_sample_age_s is not None
            and tick.growth_sample_age_s > 2.0 * float(self.params["dt_G"])
        ):
            return values, False, "stale_growth_sample"
        return values, True, None

    def _snapshot_numeric_state(self) -> dict[str, Any]:
        filters = {}
        for name, filter_ in {
            "controller": self.ekf,
            "G_measure": self.ekf_G_measure,
            "n": self.ekf_n,
            "k_0": self.ekf_k_0,
            "E_A": self.ekf_E_A,
        }.items():
            filters[name] = (
                (filter_.state.copy(), filter_.covariance.copy())
                if filter_ is not None
                else None
            )
        return {
            "history_lengths": {key: len(values) for key, values in self.history.items()},
            "integrals": (
                self.int_e_target_dt,
                self.int_e_dT_dt_dt,
                self.int_e_T_dt,
            ),
            "params": copy.deepcopy(self.params),
            "filters": filters,
            "simulation_T_j_set": (
                self.simulation_state.get("T_j_set")
                if self.simulation_state is not None
                else None
            ),
            "num_adapt": self.num_adapt,
        }

    def _restore_numeric_state(self, snapshot: Mapping[str, Any]) -> None:
        for key, length in snapshot["history_lengths"].items():
            del self.history[key][int(length) :]
        (
            self.int_e_target_dt,
            self.int_e_dT_dt_dt,
            self.int_e_T_dt,
        ) = snapshot["integrals"]
        self.params = copy.deepcopy(snapshot["params"])
        self.num_adapt = int(snapshot["num_adapt"])
        if self.simulation_state is not None:
            self.simulation_state["T_j_set"] = float(snapshot["simulation_T_j_set"])
        for name, filter_ in {
            "controller": self.ekf,
            "G_measure": self.ekf_G_measure,
            "n": self.ekf_n,
            "k_0": self.ekf_k_0,
            "E_A": self.ekf_E_A,
        }.items():
            saved = snapshot["filters"][name]
            if filter_ is not None and saved is not None:
                filter_.state = saved[0].copy()
                filter_.covariance = saved[1].copy()

    def step(self, tick_input: ControllerTickInput) -> ControllerStepResult | None:
        if not self.running:
            raise RuntimeError("MatlabController is not running.")
        if not isinstance(tick_input, ControllerTickInput):
            raise TypeError("step() requires ControllerTickInput.")
        dt = float(tick_input.controller_dt_s)
        if not math.isclose(dt, float(self.params["dt"]), rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("controller_dt_s does not match parameter dt.")
        if tick_input.tick_seq != self.frame_index + 1:
            raise ValueError("Controller tick sequence must be contiguous.")
        simulation_tick_index = self.frame_index
        self.frame_index = tick_input.tick_seq
        self.elapsed_s = float(tick_input.elapsed_s)
        rollback_state: dict[str, Any] | None = None
        try:
            if self.params["run_type"] == "simulation":
                state = self._advance_simulation(simulation_tick_index, dt)
            else:
                if tick_input.process_state is None:
                    return ControllerStepResult(valid=False, error="Experiment tick is missing ProcessState.")
                state = tick_input.process_state.to_dict()
                if self.pending_seed:
                    self.pending_seed = False
                    self.mark_seed = True
            T, T_j, c = float(state["T"]), float(state["T_j"]), float(state["c"])
            count_middle = float(state["count_middle"])
            current_T_j_set = float(state["T_j_set"])
            values = (T, T_j, c, count_middle, current_T_j_set)
            if not all(math.isfinite(value) for value in values):
                return ControllerStepResult(valid=False, error="Process input is non-finite.")
            if T <= 0 or T_j <= 0 or current_T_j_set <= 0 or c < 0 or count_middle < 0:
                return ControllerStepResult(valid=False, error="Process input violates physical bounds.")

            # MATLAB initializes the experiment EKF from the live T/c values
            # read during Controller activation. In Python the first tick is
            # the first safe point at which that complete snapshot exists.
            if self.ekf is None:
                self._initialize_filters(T, c)

            rollback_state = self._snapshot_numeric_state()
            for key, value in (("t", self.elapsed_s), ("T", T), ("T_j", T_j),
                               ("c", c), ("count_middle", count_middle)):
                self.history[key].append(value)
            dT_dt = calc_dT_dt(self.history["T"], dt)
            dc_dt = calc_dc_dt(self.history["c"], dt)
            self.history["dT_dt"].append(dT_dt)
            self.history["dc_dt"].append(dc_dt)
            if self.ekf is None:
                raise RuntimeError("Controller EKF is missing.")
            filtered = smooth_EKF(self.ekf, self.params, np.array([T, dT_dt, c, dc_dt]))
            T_KF, dT_dt_KF, c_KF, dc_dt_KF = map(float, filtered)
            for key, value in (("T_KF", T_KF), ("dT_dt_KF", dT_dt_KF),
                               ("c_KF", c_KF), ("dc_dt_KF", dc_dt_KF)):
                self.history[key].append(value)

            sigma = float(calc_sigma(c_KF, T_KF)[0])
            G_model = float(calc_G(self.params, c_KF, T_KF))
            target = str(self.params["target"])
            target_value = sigma if target == "sigma" else G_model
            target_set = float(self.params["target_set"])
            absolute_error = abs(target_value - target_set)
            for key, value in (("sigma", sigma), ("G_model", G_model),
                               ("target", target_value), ("target_set", target_set),
                               ("abs_e_target", absolute_error)):
                self.history[key].append(value)
            for name in ("sigma", "G"):
                active = target == name
                self.history[f"target_{name}"].append(target_value if active else None)
                self.history[f"target_{name}_set"].append(target_set if active else None)
                self.history[f"abs_e_target_{name}"].append(absolute_error if active else None)

            lag_percentage = calc_t_lag_perc(
                float(self.params["t_lag_perc"]), float(self.params["t_lag_threshold_perc"]),
                target_set, absolute_error,
            )
            lag = float(self.params["t_lag"]) * lag_percentage
            self.history["t_lag_perc"].append(lag_percentage)
            self.history["t_lag"].append(lag)
            projected = filtered + np.array([filtered[1], 0.0, filtered[3], 0.0]) * lag
            if abs(float(calc_relative_sigma(projected[2], projected[0]))) < float(self.params["sigma_threshold"]):
                if not self.history["T_j_set"] or self.history["T_j_set"][-1] is None:
                    # The frozen script indexes ii-1 here. At ii=1 MATLAB's
                    # outer try/catch produces no control result. Keep every
                    # history aligned while representing that warm-up as None.
                    for key in ("dT_dt_set", "T_j_set", "objective", "objective_sigma", "objective_G"):
                        self.history[key].append(None)
                    for key in ("G_u", "G_u_KF", "G_v", "G_v_KF", "G_measure", "G_measure_KF"):
                        self.history[key].append(None)
                    self.history["to_adapt"].append(False)
                    for key in ("n", "k_0", "E_A"):
                        self.history[key].append(float(self.params[key]))
                    return None
                dT_dt_set = float(self.history["dT_dt_set"][-1])
                T_j_set = float(self.history["T_j_set"][-1])
            else:
                dT_dt_set, self.int_e_target_dt = calc_dT_dt_set(
                    self.params, projected, str(self.params["mode"]), target, target_set,
                    float(self.params["dT_dt_min"]), float(self.params["dT_dt_max"]),
                    dt, self.int_e_target_dt,
                )
                T_j_set, self.int_e_dT_dt_dt, self.int_e_T_dt = calc_T_j_set(
                    self.params, projected, dT_dt_set, self.int_e_dT_dt_dt,
                    self.int_e_T_dt, T_j, float(self.params["T_j_min"]),
                    float(self.params["T_j_max"]), dt, target == "G",
                )
            objective = objective_function(self.params, filtered, target, dT_dt_set, target_set, dt)
            self.history["dT_dt_set"].append(dT_dt_set)
            self.history["T_j_set"].append(T_j_set)
            self.history["objective"].append(objective)
            for name in ("sigma", "G"):
                self.history[f"objective_{name}"].append(objective if target == name else None)
            if self.params["run_type"] == "simulation" and self.simulation_state is not None:
                self.simulation_state["T_j_set"] = T_j_set

            growth_values, adaptation_allowed, pause_reason = self._growth_values(tick_input, c_KF, T_KF)
            G_measure: float | None = None
            G_measure_KF: float | None = None
            if growth_values is not None:
                for key, value in zip(("G_u", "G_u_KF", "G_v", "G_v_KF"), growth_values):
                    self.history[key].append(value)
                G_measure = 0.5 * (growth_values[1] + growth_values[3])
            else:
                for key in ("G_u", "G_u_KF", "G_v", "G_v_KF"):
                    self.history[key].append(None)
            self.history["G_measure"].append(G_measure)
            should_adapt = self.adaptation_enabled and adaptation_allowed and G_measure is not None
            self.last_adaptation_pause_reason = None
            if should_adapt:
                if self.ekf_G_measure is None:
                    raise RuntimeError("Growth measurement EKF is missing.")
                finite_measurements = [float(value) for value in self.history["G_measure"] if value is not None]
                G_measure_KF = smooth_EKF_general(finite_measurements, self.ekf_G_measure, dt)
                self.history["G_measure_KF"].append(G_measure_KF)
                self.history["to_adapt"].append(True)
                growth_history = [float("nan") if value is None else float(value)
                                  for value in self.history["G_measure_KF"]]
                self.params, self.num_adapt = adapt_growth_parameters(
                    self.params, growth_history, self.history["sigma"], self.history["T"],
                    self.history["to_adapt"], int(self.params["max_num_adapt"]),
                    int(self.params["min_num_adapt"]), self.adaptation_mode,
                )
                self.ekf.transition = lambda value: state_transition_function(self.params, value, dt)
                self.ekf.measurement = lambda value: np.array([value[0], value[2]])
            else:
                self.history["G_measure_KF"].append(None)
                self.history["to_adapt"].append(False)
                if self.adaptation_enabled:
                    self.last_adaptation_pause_reason = pause_reason or "adaptation_not_allowed"
            for key in ("n", "k_0", "E_A"):
                self.history[key].append(float(self.params[key]))

            numerical = (T_j_set, sigma, G_model, objective, self.params["n"],
                         self.params["k_0"], self.params["E_A"])
            if not all(math.isfinite(float(value)) for value in numerical):
                return ControllerStepResult(valid=False, error="Controller calculation produced a non-finite value.")
            if not float(self.params["T_j_min"]) <= T_j_set <= float(self.params["T_j_max"]):
                return ControllerStepResult(valid=False, error="Controller setpoint violates jacket bounds.")
            return ControllerStepResult(
                T=T, T_j=T_j, c=c, dT_dt=dT_dt, dc_dt=dc_dt, T_KF=T_KF,
                dT_dt_KF=dT_dt_KF, c_KF=c_KF, dc_dt_KF=dc_dt_KF, sigma=sigma,
                G_model=G_model, G_measure=G_measure, G_measure_KF=G_measure_KF,
                target_value=target_value, target_set=target_set,
                target_error_abs=absolute_error, dT_dt_set=dT_dt_set,
                T_j_set=T_j_set, objective=objective, E_A=float(self.params["E_A"]),
                k_0=float(self.params["k_0"]), n=float(self.params["n"]),
            )
        except (OptimizationError, AdaptationError, FloatingPointError) as exc:
            if rollback_state is not None:
                self._restore_numeric_state(rollback_state)
            return ControllerStepResult(valid=False, error=str(exc))

    def stop(self) -> None:
        self.running = False

    def add_seed(self, event: Mapping[str, Any]) -> None:
        if not self.running:
            raise RuntimeError("Cannot add seed while Controller is stopped.")
        if not isinstance(event, Mapping):
            raise TypeError("Seed event must be a mapping.")
        self.pending_seed = True

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

    def export_state(self) -> Mapping[str, Any] | None:
        if not self.configured or self.run_id is None or self.params_digest is None:
            return None
        filters = {
            name: filter_.to_dict() if filter_ is not None else None
            for name, filter_ in {
                "controller": self.ekf, "G_measure": self.ekf_G_measure,
                "n": self.ekf_n, "k_0": self.ekf_k_0, "E_A": self.ekf_E_A,
            }.items()
        }
        return _json_value({
            "algorithm_state_schema_version": ALGORITHM_STATE_SCHEMA_VERSION,
            "baseline_commit": BASELINE_COMMIT, "run_id": self.run_id,
            "params_digest": self.params_digest, "running": self.running,
            "frame_index": self.frame_index, "elapsed_s": self.elapsed_s,
            "params": self.params, "history": self.history,
            "integrals": {"int_e_target_dt": self.int_e_target_dt,
                          "int_e_dT_dt_dt": self.int_e_dT_dt_dt,
                          "int_e_T_dt": self.int_e_T_dt},
            "simulation": {"state": self.simulation_state,
                           "size_list_seed": self.size_list_seed,
                           "size_list": self.size_list, "pending_seed": self.pending_seed,
                           "mark_seed": self.mark_seed},
            "filters": filters,
            "adaptation": {"enabled": self.adaptation_enabled, "mode": self.adaptation_mode,
                           "num_adapt": self.num_adapt,
                           "pause_reason": self.last_adaptation_pause_reason},
        })

    def restore_state(
        self, params: Mapping[str, Any], run_id: str, state: Mapping[str, Any]
    ) -> bool:
        try:
            if not isinstance(state, Mapping):
                return False
            if state.get("algorithm_state_schema_version") != ALGORITHM_STATE_SCHEMA_VERSION:
                return False
            if state.get("baseline_commit") != BASELINE_COMMIT or str(state.get("run_id", "")) != str(run_id):
                return False
            configured = build_parameters(params)
            if state.get("params_digest") != parameter_digest(configured):
                return False
            self.configure(params, run_id)
            self.start()
            saved_params = state.get("params")
            if not isinstance(saved_params, Mapping):
                raise ValueError("Recovery params are missing.")
            self.params = copy.deepcopy(dict(saved_params))
            history = state.get("history")
            if not isinstance(history, Mapping) or set(history) != set(HISTORY_KEYS):
                raise ValueError("Recovery history is incompatible.")
            frame_index = int(state["frame_index"])
            restored = {key: list(history[key]) for key in HISTORY_KEYS}
            history_lengths = {len(values) for values in restored.values()}
            if len(history_lengths) != 1 or next(iter(history_lengths)) > frame_index:
                raise ValueError("Recovery history lengths are incompatible.")
            self.history = restored
            for key, values in self.history.items():
                setattr(self, f"{key}_list", values)
            self.frame_index = frame_index
            self.elapsed_s = float(state["elapsed_s"])
            integrals = state["integrals"]
            self.int_e_target_dt = float(integrals["int_e_target_dt"])
            self.int_e_dT_dt_dt = float(integrals["int_e_dT_dt_dt"])
            self.int_e_T_dt = float(integrals["int_e_T_dt"])
            simulation = state["simulation"]
            self.simulation_state = ({key: float(value) for key, value in simulation["state"].items()}
                                     if simulation.get("state") is not None else None)
            self.size_list_seed = np.asarray(simulation["size_list_seed"], dtype=float)
            self.size_list = np.asarray(simulation["size_list"], dtype=float)
            self.pending_seed = bool(simulation["pending_seed"])
            self.mark_seed = bool(simulation["mark_seed"])
            adaptation = state["adaptation"]
            self.set_adaptation(bool(adaptation["enabled"]), str(adaptation["mode"]))
            self.num_adapt = int(adaptation["num_adapt"])
            self.last_adaptation_pause_reason = adaptation.get("pause_reason")
            # Rebuild closures after restoring possibly adapted E_A/k_0/n;
            # then restore the numerical state and covariance below.
            initial_T = (
                float(self.history["T"][-1])
                if self.history["T"]
                else float(self.params["T_init"])
            )
            initial_c = (
                float(self.history["c"][-1])
                if self.history["c"]
                else float(self.params["c_init"])
            )
            self._initialize_filters(initial_T, initial_c)
            filters = state["filters"]
            for filter_, name in ((self.ekf, "controller"), (self.ekf_G_measure, "G_measure"),
                                  (self.ekf_n, "n"), (self.ekf_k_0, "k_0"),
                                  (self.ekf_E_A, "E_A")):
                saved_filter = filters.get(name)
                if saved_filter is None and frame_index == 0:
                    continue
                if filter_ is None or not isinstance(saved_filter, Mapping):
                    raise ValueError("Recovery filter is missing.")
                restore_EKF(filter_, saved_filter)
            if self.params["run_type"] == "experiment" and frame_index == 0:
                self.ekf = None
                self.ekf_G_measure = None
                self.ekf_n = None
                self.ekf_k_0 = None
                self.ekf_E_A = None
            self.running = bool(state["running"])
            return True
        except (KeyError, TypeError, ValueError, OverflowError):
            self.running = False
            return False


__all__ = ["ALGORITHM_STATE_SCHEMA_VERSION", "BASELINE_COMMIT", "MatlabController"]
