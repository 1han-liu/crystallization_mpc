"""Controller parameter contract and normalization."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping


BASELINE_DEFAULTS: dict[str, Any] = {
    "mode": "MPC",
    "run_type": "simulation",
    "target": "sigma",
    "growth_rate_source": "simulated",
    "sigma_set": 0.12,
    "G_set": 3e-8,
    "dt": 5.0,
    "dt_G": 15.0,
    "C2K": 273.15,
    "K2C": -273.15,
    "k": 68.2,
    "k_loss": 6.4,
    "c_p": 4180.0,
    "r_reactor": 0.04,
    "rho_solvent": 998.0,
    "rho_solute": 1160.7,
    "m_solvent": 0.3,
    "w_sigma": 12.0,
    "w_G": 12.0,
    "w_dT_dt": 2.0,
    "var_dT_dt": 1e-7,
    "var_dc_dt": 1e-12,
    "dT": 0.01,
    "dc": 0.001,
    "R": 8.314,
    "n": 1.75,
    "E_A": 150000.0,
    "n_proc": 1.5,
    "E_A_proc": 120000.0,
    "T_R": 298.15,
    "K_P_dT_dt": 3.0,
    "K_I_dT_dt": 30.0,
    "K_P_T_sigma": 0.0,
    "K_I_T_sigma": 0.0,
    "K_P_T_G": 0.5,
    "K_I_T_G": 0.0,
    "PI_mode": "Ziegler_Nichols",
    "K_u": 15.0,
    "T_u": 60.0,
    "dT_dt_min_sigma": -0.3,
    "dT_dt_max_sigma": 0.3,
    "dT_dt_min_G": -0.5,
    "dT_dt_max_G": 0.5,
    "T_j_min": 253.15,
    "T_j_max": 453.15,
    "dT_j_dt_max": 0.2,
    "dT_j": 0.01,
    "dt_update": 0.01,
    "sigma_threshold": 0.001,
    "t_lag": 150.0,
    "t_lag_perc": 20.0 / 150.0,
    "t_lag_threshold_perc_sigma": 0.015,
    "t_lag_threshold_perc_G": 1.0,
    "T_init_sigma": 315.15,
    "T_init_G": 311.65,
    "T_j_init": 315.15,
    "T_j_set_init": 315.15,
    "c_init": 0.32,
    "steps_sigma": 900,
    "steps_G": 1620,
    "n_c2T": 3,
    "seed_time_sigma": 1200.0,
    "seed_time_G": 2.0,
    "m_seed": 0.001,
    "d_mean_seed": 0.00025,
    "d_std_seed": 0.000025,
    "min_num_adapt": 30,
    "max_num_adapt": 1000,
    "min_count_adapt": 30.0,
    "q2_G_measure": 1e-7,
    "r_diag_G_measure": [4.0, 3.0, 2.0],
    "q2_n": 1e-6,
    "r_diag_n": [4.0, 3.0, 2.0],
    "q2_k_0": 1e5,
    "r_diag_k_0": [4.0, 3.0, 2.0],
    "q2_E_A": 0.1,
    "r_diag_E_A": [4.0, 3.0, 2.0],
}


ALIASES = {
    "exp_sim": "run_type",
    "control_target": "target",
    "exp_sim_G": "growth_rate_source",
}


def _flatten_key(key: str) -> str:
    return key.removeprefix("params.")


def build_parameters(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return validated canonical parameters with reference-compatible defaults."""

    if not isinstance(raw, Mapping):
        raise TypeError("Controller parameters must be a mapping.")
    result = dict(BASELINE_DEFAULTS)
    for source_key, value in raw.items():
        key = ALIASES.get(str(source_key), _flatten_key(str(source_key)))
        result[key] = value

    source = str(result.get("growth_rate_source", "simulated"))
    result["growth_rate_source"] = {
        "simulation": "simulated",
        "experiment": "live_gsensor",
        "experiment_with_presaved": "presaved_images",
    }.get(source, source)
    result["run_type"] = str(result["run_type"])
    result["mode"] = str(result["mode"])
    result["target"] = str(result["target"])

    if result["mode"] not in {"MPC", "PI"}:
        raise ValueError("mode must be MPC or PI.")
    if result["target"] not in {"sigma", "G"}:
        raise ValueError("target must be sigma or G.")
    if result["run_type"] not in {"experiment", "simulation"}:
        raise ValueError("run_type must be experiment or simulation.")
    if result["growth_rate_source"] not in {
        "simulated",
        "live_gsensor",
        "presaved_images",
    }:
        raise ValueError("Unsupported growth_rate_source.")
    if result["run_type"] == "experiment" and result["growth_rate_source"] != "live_gsensor":
        raise ValueError("Experiment mode requires live_gsensor in v1.")

    numeric_positive = ("dt", "dt_G", "R", "rho_solute", "m_solvent")
    for key in numeric_positive:
        value = float(result[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive.")
        result[key] = value

    area_1 = 2.0 * float(result["m_solvent"]) / (
        float(result["rho_solvent"]) * float(result["r_reactor"])
    )
    area_2 = math.pi * float(result["r_reactor"]) ** 2
    result.setdefault("area_1", area_1)
    result.setdefault("area_2", area_2)
    result.setdefault(
        "tau_1",
        float(result["c_p"]) * float(result["m_solvent"])
        / (float(result["k"]) * float(result["area_1"])),
    )
    result.setdefault(
        "tau_2",
        float(result["c_p"]) * float(result["m_solvent"])
        / (float(result["k_loss"]) * float(result["area_2"])),
    )
    kinetic_denominator = 0.035**1.5 * math.exp(
        -120000.0 / float(result["R"]) / 306.15
    )
    result.setdefault("k_0", 0.1 * 3e-8 / kinetic_denominator)
    result.setdefault("k_0_proc", 3e-8 / kinetic_denominator)
    result.setdefault("K_P_target", calc_K_P_target(result))
    result.setdefault("K_I_target", calc_K_I_target(result))
    result["rho_solute"] = float(result["rho_solute"])

    target = result["target"]
    suffix = "sigma" if target == "sigma" else "G"
    result.setdefault("target_set", result[f"{target}_set"])
    for destination in (
        "K_P_T",
        "K_I_T",
        "dT_dt_min",
        "dT_dt_max",
        "t_lag_threshold_perc",
        "T_init",
        "steps",
        "seed_time",
    ):
        result.setdefault(destination, result[f"{destination}_{suffix}"])

    for key, value in list(result.items()):
        if isinstance(value, bool) or isinstance(value, (str, list, tuple, dict)):
            continue
        if isinstance(value, (int, float)):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"{key} must be finite.")
    return result


def calc_K_P_target(params: Mapping[str, Any]) -> float:
    if params.get("PI_mode") != "Ziegler_Nichols":
        raise ValueError("Unsupported PI_mode.")
    return 0.45 * float(params["K_u"])


def calc_K_I_target(params: Mapping[str, Any]) -> float:
    if params.get("PI_mode") != "Ziegler_Nichols":
        raise ValueError("Unsupported PI_mode.")
    return 0.0


def parameter_digest(params: Mapping[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "BASELINE_DEFAULTS",
    "build_parameters",
    "calc_K_I_target",
    "calc_K_P_target",
    "parameter_digest",
]
