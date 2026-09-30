"""Content checks for the single supported algorithm checkpoint format.

An old numeric format label is deliberately irrelevant. No missing numerical
state is reconstructed here, and validation never changes the supplied archive.
"""
from __future__ import annotations

import math
from typing import Any, Mapping


def _number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Checkpoint {name} must be a number.")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"Checkpoint {name} is outside its valid range.")
    return number


def _integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"Checkpoint {name} must be a nonnegative integer.")
    return value


def _boolean(value: Any, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"Checkpoint {name} must be a boolean.")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Checkpoint {name} must be an object.")
    return value


def _vector(value: Any, name: str, *, minimum: float | None = None) -> list:
    if not isinstance(value, list):
        raise ValueError(f"Checkpoint {name} must be an array.")
    for item in value:
        _number(item, name, minimum=minimum)
    return value


def _parameter_shape(value: Any, expected: Any, name: str) -> None:
    """Check serialized parameter types without filling defaults or changing values."""
    if isinstance(expected, bool):
        _boolean(value, name)
    elif isinstance(expected, (int, float)):
        _number(value, name)
    elif isinstance(expected, str):
        if not isinstance(value, str):
            raise ValueError(f"Checkpoint {name} must be text.")
    elif isinstance(expected, (list, tuple)):
        if not isinstance(value, list) or len(value) != len(expected):
            raise ValueError(f"Checkpoint {name} has an incompatible shape.")
        for actual, template in zip(value, expected):
            _parameter_shape(actual, template, name)
    elif isinstance(expected, Mapping):
        actual = _mapping(value, name)
        for key, template in expected.items():
            _parameter_shape(actual[key], template, f"{name}.{key}")


def validate_checkpoint(
    state: Mapping[str, Any], configured: Mapping[str, Any], history_keys: tuple[str, ...]
) -> None:
    """Raise on incomplete/invalid content, including superficially compatible archives."""
    frame = _integer(state["frame_index"], "frame_index")
    elapsed = _number(state["elapsed_s"], "elapsed_s", minimum=0)
    if frame == 0 and elapsed != 0:
        raise ValueError("Checkpoint initial elapsed time must be zero.")
    _boolean(state["running"], "running")
    if _number(state["control_T_j_reference_K"], "activation reference") <= 0:
        raise ValueError("Checkpoint activation temperature must be positive.")

    params = _mapping(state["params"], "params")
    for key, template in configured.items():
        _parameter_shape(params[key], template, f"params.{key}")
    # These fields may legitimately change through runtime control or fitting.
    mutable = {"target", "target_set", "sigma_set", "G_set", "n", "k_0", "E_A",
               "K_P_T", "K_I_T", "dT_dt_min", "dT_dt_max", "t_lag_threshold_perc"}
    for key, initial in configured.items():
        if key not in mutable and params[key] != initial:
            raise ValueError(f"Checkpoint immutable parameter {key} does not match.")
    if params["target"] not in {"sigma", "G"}:
        raise ValueError("Checkpoint control target is invalid.")
    for key in ("target_set", "sigma_set", "G_set"):
        if _number(params[key], key) <= 0:
            raise ValueError("Checkpoint target values must be positive.")
    if params["dT_dt_min"] > params["dT_dt_max"]:
        raise ValueError("Checkpoint rate bounds are inverted.")

    history = _mapping(state["history"], "history")
    if set(history) != set(history_keys):
        raise ValueError("Checkpoint history fields are incompatible.")
    # None is intentional for inactive targets, unavailable growth, and warm-up.
    nullable = {"dT_dt_set", "T_j_set", "objective", "target_sigma", "target_sigma_set",
                "abs_e_target_sigma", "objective_sigma", "target_G", "target_G_set",
                "abs_e_target_G", "objective_G", "G_u", "G_u_KF", "G_v", "G_v_KF",
                "G_measure", "G_measure_KF"}
    for key, values in history.items():
        if not isinstance(values, list):
            raise ValueError(f"Checkpoint history {key} must be an array.")
        for value in values:
            if key == "to_adapt":
                _boolean(value, key)
            elif value is not None or key not in nullable:
                _number(value, key)
    length = len(history["t"])
    if length > frame or any(len(history[key]) != length for key in history_keys
                             if key not in {"n", "k_0", "E_A"}):
        raise ValueError("Checkpoint history lengths are inconsistent.")
    if len({len(history[key]) for key in ("n", "k_0", "E_A")}) != 1 or len(history["n"]) > length:
        raise ValueError("Checkpoint parameter history lengths are inconsistent.")
    times = history["t"]
    if any(t < 0 or t > elapsed for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Checkpoint history times are inconsistent.")

    integrals = _mapping(state["integrals"], "integrals")
    for key in ("int_e_target_dt", "int_e_dT_dt_dt", "int_e_T_dt"):
        _number(integrals[key], key)
    simulation = _mapping(state["simulation"], "simulation")
    process = _mapping(simulation["state"], "simulation.state")
    for key in ("T", "T_j", "T_j_set", "c", "count_middle"):
        number = _number(process[key], key, minimum=0)
        if key in {"T", "T_j", "T_j_set"} and number == 0:
            raise ValueError("Checkpoint process temperature must be positive.")
    seeds = _vector(simulation["size_list_seed"], "size_list_seed", minimum=0)
    sizes = _vector(simulation["size_list"], "size_list", minimum=0)
    if not seeds or any(value == 0 for value in seeds) or len(seeds) != len(sizes):
        raise ValueError("Checkpoint crystal size arrays are incompatible.")
    for key in ("pending_seed", "mark_seed"):
        _boolean(simulation[key], key)

    adaptation = _mapping(state["adaptation"], "adaptation")
    _boolean(adaptation["enabled"], "adaptation.enabled")
    modes = {"E_A", "k_0", "n", "E_A_and_k_0", "E_A_and_n", "k_0_and_n", "all"}
    if not isinstance(adaptation["mode"], str) or adaptation["mode"] not in modes:
        raise ValueError("Checkpoint adaptation mode is invalid.")
    count = _integer(adaptation["num_adapt"], "num_adapt")
    # This is the last returned fitting-window count. A failed fit preserves
    # that value; do not recompute it from the current eligibility flags.
    if count > min(length, int(params["max_num_adapt"])):
        raise ValueError("Checkpoint adaptation count exceeds its history/window.")
    fitting = _vector(adaptation["fitting_G_measure_history"], "fitting history")
    assigned = [i for i, selected in enumerate(history["to_adapt"]) if selected]
    if len(fitting) != (assigned[-1] + 1 if assigned else 0):
        raise ValueError("Checkpoint fitting history length is inconsistent.")
    if any(value != 0 for i, value in enumerate(fitting) if not history["to_adapt"][i]):
        raise ValueError("Checkpoint fitting history gaps must remain zero.")
    if any(fitting[i] != history["G_measure"][i] for i in assigned):
        raise ValueError("Checkpoint fitting samples disagree with recorded measurements.")
    for key in ("pause_reason", "control_hold_reason"):
        if adaptation[key] is not None and not isinstance(adaptation[key], str):
            raise ValueError("Checkpoint pause reason must be text or null.")
    diagnostics = _mapping(adaptation["diagnostics"], "diagnostics")
    if diagnostics["last_status"] not in ("not_run", "waiting_for_samples", "fit_succeeded", "fit_failed"):
        raise ValueError("Checkpoint fitting status is invalid.")
    for key in ("fit_count", "failure_count"):
        if _integer(diagnostics[key], key) > frame:
            raise ValueError("Checkpoint fitting count exceeds elapsed ticks.")
    if diagnostics["last_tick"] is not None and _integer(diagnostics["last_tick"], "last_tick") > frame:
        raise ValueError("Checkpoint fitting tick exceeds elapsed ticks.")
    if diagnostics["last_mode"] is not None and diagnostics["last_mode"] not in tuple(modes):
        raise ValueError("Checkpoint fitting mode is invalid.")

    filters = _mapping(state["filters"], "filters")
    for key, dimension in (("controller", 4), ("G_measure", 3), ("n", 3), ("k_0", 3), ("E_A", 3)):
        filter_state = _mapping(filters[key], f"filters.{key}")
        vector = _vector(filter_state["state"], f"{key}.state")
        covariance = filter_state["covariance"]
        if len(vector) != dimension or not isinstance(covariance, list) or len(covariance) != dimension:
            raise ValueError("Checkpoint filter dimensions are incompatible.")
        for row in covariance:
            if len(_vector(row, f"{key}.covariance")) != dimension:
                raise ValueError("Checkpoint covariance dimensions are incompatible.")
