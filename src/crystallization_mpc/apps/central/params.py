from __future__ import annotations

import math
import os
from typing import Dict, Tuple, Optional

ROLE = "central"


def _require_yaml():
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise RuntimeError("pyyaml is required to load params_default.yaml") from exc
    return yaml


def _list_to_dict(items) -> Dict[str, object]:
    params: Dict[str, object] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key:
            continue
        params[str(key)] = item.get("default")
    return params


def load_params(path: str) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
    if not os.path.exists(path):
        print(f"[{ROLE}] params file not found: {path}")
        return {}, {}, {}, 1
    yaml = _require_yaml()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    params = data.get("params", {}) or {}
    version = int(data.get("version", 1))
    shared = _list_to_dict(params.get("shared"))
    gsensor = _list_to_dict(params.get("gsensor"))
    controller = _list_to_dict(params.get("controller"))
    return shared, gsensor, controller, version


def _as_float(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _compute_area_1(params: Dict[str, object]) -> Optional[float]:
    m_solvent = _as_float(params.get("m_solvent"))
    rho_solvent = _as_float(params.get("rho_solvent"))
    r_reactor = _as_float(params.get("r_reactor"))
    if m_solvent is None or rho_solvent is None or r_reactor is None:
        return None
    denom = rho_solvent * r_reactor
    if denom == 0:
        return None
    return 2.0 * m_solvent / denom


def _compute_tau_1(params: Dict[str, object]) -> Optional[float]:
    c_p = _as_float(params.get("c_p"))
    m_solvent = _as_float(params.get("m_solvent"))
    k = _as_float(params.get("k"))
    area_1 = _as_float(params.get("area_1"))
    if c_p is None or m_solvent is None or k is None or area_1 is None:
        return None
    denom = k * area_1
    if denom == 0:
        return None
    return (c_p * m_solvent) / denom


def _compute_area_2(params: Dict[str, object]) -> Optional[float]:
    r_reactor = _as_float(params.get("r_reactor"))
    if r_reactor is None:
        return None
    return math.pi * (r_reactor ** 2)


def _compute_tau_2(params: Dict[str, object]) -> Optional[float]:
    c_p = _as_float(params.get("c_p"))
    m_solvent = _as_float(params.get("m_solvent"))
    k_loss = _as_float(params.get("k_loss"))
    area_2 = _as_float(params.get("area_2"))
    if c_p is None or m_solvent is None or k_loss is None or area_2 is None:
        return None
    denom = k_loss * area_2
    if denom == 0:
        return None
    return (c_p * m_solvent) / denom


def _compute_k_0(params: Dict[str, object]) -> Optional[float]:
    r_gas = _as_float(params.get("params.R"))
    if r_gas is None:
        return None
    denom = (0.035 ** 1.5) * math.exp(-120e3 / r_gas / 306.15)
    if denom == 0:
        return None
    return 0.1 * 3.0e-8 / denom


def _compute_k_0_proc(params: Dict[str, object]) -> Optional[float]:
    r_gas = _as_float(params.get("params.R"))
    if r_gas is None:
        return None
    denom = (0.035 ** 1.5) * math.exp(-120e3 / r_gas / 306.15)
    if denom == 0:
        return None
    return 3.0e-8 / denom


def apply_derived_params(
    shared: Dict[str, object],
    controller: Dict[str, object],
    target: Optional[str] = None,
) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object]]:
    """
    Returns (shared, controller_with_derived, derived_only).
    Derived params are only added when missing and when dependencies exist.
    """
    merged = {**shared, **controller}
    derived: Dict[str, object] = {}

    if "area_1" not in merged:
        area_1 = _compute_area_1(merged)
        if area_1 is not None:
            derived["area_1"] = area_1

    merged_with_derived = {**merged, **derived}
    if "params.tau_1" not in merged_with_derived:
        tau_1 = _compute_tau_1(merged_with_derived)
        if tau_1 is not None:
            derived["params.tau_1"] = tau_1

    merged_with_derived = {**merged_with_derived, **derived}
    if "area_2" not in merged_with_derived:
        area_2 = _compute_area_2(merged_with_derived)
        if area_2 is not None:
            derived["area_2"] = area_2

    merged_with_derived = {**merged_with_derived, **derived}
    if "params.tau_2" not in merged_with_derived:
        tau_2 = _compute_tau_2(merged_with_derived)
        if tau_2 is not None:
            derived["params.tau_2"] = tau_2

    merged_with_derived = {**merged_with_derived, **derived}
    if "params.rho_solute" not in merged_with_derived and "rho_solute" in merged_with_derived:
        derived["params.rho_solute"] = merged_with_derived["rho_solute"]

    merged_with_derived = {**merged_with_derived, **derived}
    if "params.k_0" not in merged_with_derived:
        k_0 = _compute_k_0(merged_with_derived)
        if k_0 is not None:
            derived["params.k_0"] = k_0

    merged_with_derived = {**merged_with_derived, **derived}
    if "params.k_0_proc" not in merged_with_derived:
        k_0_proc = _compute_k_0_proc(merged_with_derived)
        if k_0_proc is not None:
            derived["params.k_0_proc"] = k_0_proc

    if target in ("sigma", "G"):
        suffix = "sigma" if target == "sigma" else "G"
        mapping = {
            "target_set": f"{suffix}_set",
            "params.K_P_T": f"params.K_P_T_{suffix}",
            "params.K_I_T": f"params.K_I_T_{suffix}",
            "dT_dt_min": f"dT_dt_min_{suffix}",
            "dT_dt_max": f"dT_dt_max_{suffix}",
            "t_lag_threshold_perc": f"t_lag_threshold_perc_{suffix}",
            "T_init": f"T_init_{suffix}",
            "steps": f"steps_{suffix}",
            "seed_time": f"seed_time_{suffix}",
        }
        merged_with_derived = {**merged_with_derived, **derived}
        for dst, src in mapping.items():
            if dst in merged_with_derived:
                continue
            if src in merged_with_derived:
                derived[dst] = merged_with_derived[src]

    controller_with_derived = {**controller, **derived}
    return shared, controller_with_derived, derived
