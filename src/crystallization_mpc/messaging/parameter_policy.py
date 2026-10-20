"""Explicit, shared policy for the 91 editable parameter-panel fields.

Wire keys retain their original section and spelling.  The registry is closed:
new fields are not silently made editable when a default document changes.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any


_DEFAULTS = {
    "shared": {"dt": 5, "dt_G": 15, "resolution": 8.676e-7,
               "q2": 1e-16, "r_diag": [4, 3, 2]},
    "gsensor": {
        "alignment_method": "none", "ptr_format": ["%d", "%05d", "%d"],
        "params_G.width": 100, "params_G.ratio": 10, "params_G.delta_theta": 10,
        "params_G.width_divider": 4, "params_G.num_peak": 10, "params_G.len_min": 50,
        "params_G.hough_threshold": 10, "params_G.hough_min_line_length": 7,
        "params_G.hough_max_line_gap": 5,
    },
    "controller": {
        "sigma_set": .035, "G_set": 3e-8, "params.var_dT_dt": 1e-7,
        "params.var_dc_dt": 1e-12, "params.dT": .01, "params.dc": .001,
        "params.n": 1.75, "params.E_A": 150000, "params.T_R": 298.15,
        "k": 68.2, "rho_solute": 1160.7, "m_solvent": .3,
        "params.w_sigma": 12, "params.w_G": 12, "params.w_dT_dt": 2,
        "sigma_threshold": .001, "params.PI_mode": "Ziegler_Nichols",
        "params.K_u": 15, "params.T_u": 60, "params.K_P_dT_dt": 3,
        "params.K_I_dT_dt": 30, "params.K_P_T_sigma": 0, "params.K_I_T_sigma": 0,
        "params.K_P_T_G": .5, "params.K_I_T_G": 0, "params.n_proc": 1.5,
        "params.E_A_proc": 120000, "t_lag": 150, "t_lag_perc": 20 / 150,
        "t_lag_threshold_perc_sigma": .015, "t_lag_threshold_perc_G": 1,
        "dT_dt_min_sigma": -.3, "dT_dt_max_sigma": .3,
        "dT_dt_min_G": -.5, "dT_dt_max_G": .5,
        "T_j_min": 253.15, "T_j_max": 453.15,
        "min_num_adapt": 15, "max_num_adapt": 50, "min_count_adapt": 30,
        "q2_G_measure": 1e-7, "r_diag_G_measure": [4, 3, 2],
        "q2_n": 1e-6, "r_diag_n": [4, 3, 2],
        "q2_k_0": 1e5, "r_diag_k_0": [4, 3, 2],
        "q2_E_A": .1, "r_diag_E_A": [4, 3, 2],
        "T_init_sigma": 315.15, "T_init_G": 311.65,
        "T_j_init": 315.15, "T_j_set_init": 315.15, "c_init": .32,
        "dT_j_dt_max": .2, "dT_j": .01, "dt_update": .01,
        "steps_sigma": 900, "steps_G": 1620, "n_c2T": 3,
        "seed_time_sigma": 1200, "seed_time_G": 2,
        "m_seed": .001, "d_mean_seed": .00025, "d_std_seed": .000025,
        "t_base": 60, "t_string": "min", "params.R": 8.314,
        "C2K": 273.15, "K2C": -273.15, "c_p": 4180,
        "rho_solvent": 998, "r_reactor": .04, "k_loss": 6.4,
        "delta_e_target_sigma_threshold": .005,
        "delta_e_target_G_threshold": 5e-9,
    },
}
STARTUP_KEYS = frozenset({"T_init_sigma", "T_init_G", "T_j_init", "T_j_set_init",
                         "c_init", "steps_sigma", "steps_G", "seed_time_sigma", "seed_time_G"})
UNUSED_CONTROLLER_KEYS = frozenset({"params.T_u", "params.K_P_T_sigma", "params.K_P_T_G",
                                   "min_count_adapt", "n_c2T", "t_base", "t_string",
                                   "C2K", "K2C", "delta_e_target_sigma_threshold",
                                   "delta_e_target_G_threshold"})
RESERVED_FILTER_KEYS = frozenset({"q2_n", "r_diag_n", "q2_k_0", "r_diag_k_0",
                                "q2_E_A", "r_diag_E_A"})
NEXT_SEED_KEYS = frozenset({"m_seed", "d_mean_seed", "d_std_seed"})
SIMULATION_ONLY_KEYS = frozenset({"params.n_proc", "params.E_A_proc", "dT_j_dt_max",
                                 "dT_j", "dt_update"})
_DERIVED = ("area_1", "area_2", "tau_1", "tau_2", "k_0", "k_0_proc", "K_P_target",
            "K_I_target", "target_set", "K_P_T", "K_I_T", "dT_dt_min", "dT_dt_max",
            "t_lag_threshold_perc", "T_init", "steps", "seed_time")
_SIGNED = frozenset({"dT_dt_min_sigma", "dT_dt_max_sigma", "dT_dt_min_G", "dT_dt_max_G",
                     "K2C"})
_ZERO = frozenset({"q2", "params.var_dT_dt", "params.var_dc_dt", "params.w_sigma",
                   "params.w_G", "params.w_dT_dt", "sigma_threshold", "params.K_u",
                   "params.K_P_dT_dt", "params.K_I_dT_dt", "params.K_P_T_sigma",
                   "params.K_I_T_sigma", "params.K_P_T_G", "params.K_I_T_G",
                   "t_lag", "t_lag_perc", "t_lag_threshold_perc_sigma",
                   "t_lag_threshold_perc_G", "min_count_adapt", "q2_G_measure", "q2_n",
                   "q2_k_0", "q2_E_A", "d_std_seed", "params_G.hough_max_line_gap",
                   "delta_e_target_sigma_threshold", "delta_e_target_G_threshold"})
_INTEGERS = frozenset({"params_G.width", "params_G.width_divider", "params_G.num_peak",
                       "params_G.len_min", "params_G.hough_threshold",
                       "params_G.hough_min_line_length", "params_G.hough_max_line_gap",
                       "min_num_adapt", "max_num_adapt", "steps_sigma", "steps_G", "n_c2T"})


def parameter_defaults() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(_DEFAULTS)


def parameter_policies() -> dict[str, dict[str, dict[str, Any]]]:
    policies = {}
    for section, values in _DEFAULTS.items():
        policies[section] = {}
        for key, default in values.items():
            effect, reason = "controller_refresh", "Applied at a control-cycle boundary; Controller state is refreshed."
            if section == "gsensor" or (section == "shared" and key != "dt"):
                effect, reason = "measurement_segment", "Starts a new measurement segment; external image timing must match dt_G."
            if section == "controller" and key in STARTUP_KEYS:
                effect, reason = "startup_only", "Used only for experiment initialization."
            elif (section == "controller" and key in UNUSED_CONTROLLER_KEYS) or key in {"ptr_format", "params_G.ratio"}:
                effect, reason = "unused", "Recorded for this experiment; not used by the current calculation."
            elif section == "controller" and key in RESERVED_FILTER_KEYS:
                effect, reason = "reserved_filter", "Reconstructs a reserved filter; the filter is not currently updated."
            elif section == "controller" and key in NEXT_SEED_KEYS:
                effect, reason = "next_seed", "Simulation only: affects the next explicit seed addition, not existing crystals; at most 1,000,000 simulated crystals."
            elif section == "controller" and key in SIMULATION_ONLY_KEYS:
                effect, reason = "simulation_only", "Affects future internal simulation steps; does not change external equipment."
                if key == "dt_update":
                    reason += " At most 100,000 internal jacket substeps per control cycle."
            policies[section][key] = {
                "runtime_editable": effect != "startup_only", "effect": effect, "reason": reason,
                "kind": "array" if isinstance(default, list) else "string" if isinstance(default, str) else "integer" if key in _INTEGERS else "number",
            }
    return policies


def derived_parameter_policies() -> dict[str, dict[str, Any]]:
    return {key: {"runtime_editable": False, "effect": "derived", "reason": "Calculated from editable parameters."}
            for key in _DERIVED}


def _value(key: str, value: Any) -> Any:
    if key == "alignment_method":
        if value not in ("none", "centroid", "fft", "kalman", "loftr", "sift", "optical_flow", "ecc"):
            raise ValueError("Unsupported alignment_method.")
        return value
    if key == "params.PI_mode":
        if value != "Ziegler_Nichols":
            raise ValueError("Unsupported PI_mode.")
        return value
    if key == "t_string":
        if value not in ("s", "sec", "min", "h"):
            raise ValueError("t_string must be s, sec, min or h.")
        return value
    if key == "ptr_format":
        if not isinstance(value, (list, tuple)) or len(value) != 3 or any(not isinstance(item, str) or not item for item in value):
            raise ValueError("ptr_format must contain three nonempty format strings.")
        return list(value)
    if key.startswith("r_diag"):
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError(f"{key} must contain three positive numbers.")
        return [_number(key, item, positive=True) for item in value]
    number = _number(key, value, positive=key not in _ZERO and key not in _SIGNED)
    if key in _ZERO and number < 0:
        raise ValueError(f"{key} must be nonnegative.")
    if key in _INTEGERS:
        if not number.is_integer():
            raise ValueError(f"{key} must be an integer.")
        number = int(number)
    if key == "params_G.delta_theta" and number > 180:
        raise ValueError("params_G.delta_theta cannot exceed 180 degrees.")
    return number


def _number(key: str, value: Any, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number.")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{key} must be finite{' and positive' if positive else ''}.")
    return value


def validate_parameter_changes(changes: Mapping[str, Any], current: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Normalize a closed raw-key patch without mutating values or defaults.

    Supply ``current`` for complete candidate dependency checks.  Without it,
    cross-field checks use defaults only when both relevant fields are patched.
    """
    if not isinstance(changes, Mapping):
        raise ValueError("Parameter changes must be a section mapping.")
    policies = parameter_policies()
    result = {}
    for section, patch in changes.items():
        if section not in policies or not isinstance(patch, Mapping):
            raise ValueError(f"Unknown or invalid parameter section: {section}.")
        result[section] = {}
        for key, value in patch.items():
            if key not in policies[section]:
                raise ValueError(f"Unknown or derived parameter: {section}.{key}.")
            if not policies[section][key]["runtime_editable"]:
                raise ValueError(f"{section}.{key} is startup-only.")
            result[section][key] = _value(key, value)
        if not result[section]:
            result.pop(section)
    candidate = parameter_defaults()
    if current is not None:
        for section in candidate:
            values = current.get(section, {})
            if not isinstance(values, Mapping):
                raise ValueError(f"Current parameter section {section} must be an object.")
            candidate[section].update(values)
    for section, patch in result.items():
        candidate[section].update(patch)
    for lower, upper in (("T_j_min", "T_j_max"), ("dT_dt_min_sigma", "dT_dt_max_sigma"),
                         ("dT_dt_min_G", "dT_dt_max_G"), ("min_num_adapt", "max_num_adapt")):
        patched = result.get("controller", {})
        if (current is not None and (lower in patched or upper in patched)) or (lower in patched and upper in patched):
            if candidate["controller"][lower] > candidate["controller"][upper] or (lower != "min_num_adapt" and candidate["controller"][lower] == candidate["controller"][upper]):
                raise ValueError(f"{lower} must {'not exceed' if lower == 'min_num_adapt' else 'be below'} {upper}.")
    return result


def measurement_parameter_changes(changes: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    normalized = validate_parameter_changes(changes)
    result = {}
    for section, patch in normalized.items():
        selected = {key: value for key, value in patch.items()
                    if (section == "shared" and key != "dt") or (section == "gsensor" and key not in {"ptr_format", "params_G.ratio"})}
        if selected:
            result[section] = selected
    return result


__all__ = ["parameter_defaults", "parameter_policies", "derived_parameter_policies",
           "validate_parameter_changes", "measurement_parameter_changes"]
