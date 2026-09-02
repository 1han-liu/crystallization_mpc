from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

ROLE = "central"
TARGET_SWITCH_KEYS = (
    ("target_set", "sigma_set", "G_set"),
    ("params.K_P_T", "params.K_P_T_sigma", "params.K_P_T_G"),
    ("params.K_I_T", "params.K_I_T_sigma", "params.K_I_T_G"),
    ("dT_dt_min", "dT_dt_min_sigma", "dT_dt_min_G"),
    ("dT_dt_max", "dT_dt_max_sigma", "dT_dt_max_G"),
    ("t_lag_threshold_perc", "t_lag_threshold_perc_sigma", "t_lag_threshold_perc_G"),
    ("T_init", "T_init_sigma", "T_init_G"),
    ("steps", "steps_sigma", "steps_G"),
    ("seed_time", "seed_time_sigma", "seed_time_G"),
)

POSITIVE_PARAM_KEYS = {
    "dt",
    "dt_G",
    "resolution",
    "params_G.width",
    "params_G.ratio",
    "params_G.delta_theta",
    "params_G.width_divider",
    "params_G.num_peak",
    "params_G.len_min",
    "params_G.hough_threshold",
    "params_G.hough_min_line_length",
}
NONNEGATIVE_PARAM_KEYS = {
    "q2",
    "params_G.hough_max_line_gap",
}


class ParameterValidationError(ValueError):
    """Raised when a Central parameter draft cannot be saved or applied."""


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


def _dict_to_list(params: Dict[str, object]) -> List[Dict[str, object]]:
    return [{"key": key, "default": value} for key, value in params.items()]


def load_params_document(path: str) -> Dict[str, object]:
    if not os.path.exists(path):
        print(f"[{ROLE}] params file not found: {path}")
        return {"version": 1, "params": {"shared": [], "gsensor": [], "controller": []}}
    yaml = _require_yaml()
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_param_meta(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    yaml = _require_yaml()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    params = data.get("params", {}) or {}
    return {str(key): dict(value or {}) for key, value in params.items()}


def load_operation_meta(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    yaml = _require_yaml()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sections = data.get("sections", []) or []
    result: List[Dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        result.append(
            {
                "title": str(section.get("title", "")),
                "items": list(section.get("items", []) or []),
            }
        )
    return result


def save_params_document(
    path: str,
    version: int,
    shared: Dict[str, object],
    gsensor: Dict[str, object],
    controller: Dict[str, object],
) -> None:
    if isinstance(version, bool) or int(version) < 1:
        raise ParameterValidationError("Parameter version must be a positive integer.")
    yaml = _require_yaml()
    data = {
        "version": version,
        "params": {
            "shared": _dict_to_list(shared),
            "gsensor": _dict_to_list(gsensor),
            "controller": _dict_to_list(controller),
        },
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_params(path: str) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
    data = load_params_document(path)
    params = data.get("params", {}) or {}
    version = int(data.get("version", 1))
    shared = _list_to_dict(params.get("shared"))
    gsensor = _list_to_dict(params.get("gsensor"))
    controller = _list_to_dict(params.get("controller"))
    return shared, gsensor, controller, version


def validate_params_section(
    section_name: str,
    submitted: Dict[str, object],
    defaults: Dict[str, object],
    meta: Dict[str, Dict[str, Any]],
) -> Dict[str, object]:
    """Validate one complete UI section and return normalized JSON-safe values."""

    unknown = sorted(set(submitted) - set(defaults))
    if unknown:
        raise ParameterValidationError(
            f"Unknown {section_name} parameter(s): {', '.join(unknown)}."
        )

    required = {
        key
        for key in defaults
        if not bool(meta.get(key, {}).get("derived", False))
        and (meta.get(key, {}).get("ui", {}) or {}).get("visible", True) is not False
    }
    missing = sorted(required - set(submitted))
    if missing:
        raise ParameterValidationError(
            f"Missing {section_name} parameter(s): {', '.join(missing)}."
        )

    normalized = dict(defaults)
    for key, value in submitted.items():
        normalized[key] = _normalize_parameter_value(
            key,
            value,
            defaults[key],
            meta.get(key, {}),
        )
    return normalized


def _normalize_parameter_value(
    key: str,
    value: object,
    expected: object,
    meta: Dict[str, Any],
) -> object:
    if expected is None:
        if value is None or isinstance(value, (str, int, float, bool, list, dict)):
            return value
        raise ParameterValidationError(f"Parameter {key} contains an unsupported value.")

    if isinstance(expected, bool):
        if not isinstance(value, bool):
            raise ParameterValidationError(f"Parameter {key} must be a boolean.")
        return value

    if isinstance(expected, int) and not isinstance(expected, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterValidationError(f"Parameter {key} must be an integer.")
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ParameterValidationError(f"Parameter {key} must be a finite integer.")
        normalized: object = int(number)
        _validate_numeric_range(key, float(normalized), meta)
        return normalized

    if isinstance(expected, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterValidationError(f"Parameter {key} must be a number.")
        number = float(value)
        if not math.isfinite(number):
            raise ParameterValidationError(f"Parameter {key} must be finite.")
        _validate_numeric_range(key, number, meta)
        return number

    if isinstance(expected, str):
        if not isinstance(value, str):
            raise ParameterValidationError(f"Parameter {key} must be text.")
        return value

    if isinstance(expected, list):
        if not isinstance(value, list):
            raise ParameterValidationError(f"Parameter {key} must be a list.")
        if len(value) != len(expected):
            raise ParameterValidationError(
                f"Parameter {key} must contain {len(expected)} item(s)."
            )
        return [
            _normalize_parameter_value(f"{key}[{index}]", item, expected[index], {})
            for index, item in enumerate(value)
        ]

    if isinstance(expected, dict):
        if not isinstance(value, dict):
            raise ParameterValidationError(f"Parameter {key} must be an object.")
        return value

    if not isinstance(value, type(expected)):
        raise ParameterValidationError(
            f"Parameter {key} must have type {type(expected).__name__}."
        )
    return value


def _validate_numeric_range(key: str, value: float, meta: Dict[str, Any]) -> None:
    base_key = key.split("[", 1)[0]
    if base_key in POSITIVE_PARAM_KEYS and value <= 0:
        raise ParameterValidationError(f"Parameter {base_key} must be greater than zero.")
    if base_key in NONNEGATIVE_PARAM_KEYS and value < 0:
        raise ParameterValidationError(f"Parameter {base_key} cannot be negative.")
    if base_key == "r_diag" and value <= 0:
        raise ParameterValidationError("Parameter r_diag values must be greater than zero.")

    minimum = meta.get("min")
    maximum = meta.get("max")
    if minimum is not None and value < float(minimum):
        raise ParameterValidationError(f"Parameter {base_key} must be at least {minimum}.")
    if maximum is not None and value > float(maximum):
        raise ParameterValidationError(f"Parameter {base_key} must be at most {maximum}.")

    if base_key == "params_G.delta_theta" and value > 180:
        raise ParameterValidationError("Parameter params_G.delta_theta cannot exceed 180.")


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


def _is_missing(params: Dict[str, object], key: str) -> bool:
    return key not in params or params.get(key) is None


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


def _compute_k_p_target(params: Dict[str, object]) -> Optional[float]:
    pi_mode = params.get("params.PI_mode")
    k_u = _as_float(params.get("params.K_u"))
    if pi_mode != "Ziegler_Nichols" or k_u is None:
        return None
    return 0.45 * k_u


def _compute_k_i_target(params: Dict[str, object]) -> Optional[float]:
    pi_mode = params.get("params.PI_mode")
    if pi_mode != "Ziegler_Nichols":
        return None
    return 0.0


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

    if _is_missing(merged, "area_1"):
        area_1 = _compute_area_1(merged)
        if area_1 is not None:
            derived["area_1"] = area_1

    merged_with_derived = {**merged, **derived}
    if _is_missing(merged_with_derived, "params.tau_1"):
        tau_1 = _compute_tau_1(merged_with_derived)
        if tau_1 is not None:
            derived["params.tau_1"] = tau_1

    merged_with_derived = {**merged_with_derived, **derived}
    if _is_missing(merged_with_derived, "area_2"):
        area_2 = _compute_area_2(merged_with_derived)
        if area_2 is not None:
            derived["area_2"] = area_2

    merged_with_derived = {**merged_with_derived, **derived}
    if _is_missing(merged_with_derived, "params.tau_2"):
        tau_2 = _compute_tau_2(merged_with_derived)
        if tau_2 is not None:
            derived["params.tau_2"] = tau_2

    merged_with_derived = {**merged_with_derived, **derived}
    if _is_missing(merged_with_derived, "params.rho_solute") and "rho_solute" in merged_with_derived:
        derived["params.rho_solute"] = merged_with_derived["rho_solute"]

    merged_with_derived = {**merged_with_derived, **derived}
    if _is_missing(merged_with_derived, "params.k_0"):
        k_0 = _compute_k_0(merged_with_derived)
        if k_0 is not None:
            derived["params.k_0"] = k_0

    merged_with_derived = {**merged_with_derived, **derived}
    if _is_missing(merged_with_derived, "params.k_0_proc"):
        k_0_proc = _compute_k_0_proc(merged_with_derived)
        if k_0_proc is not None:
            derived["params.k_0_proc"] = k_0_proc

    merged_with_derived = {**merged_with_derived, **derived}
    if _is_missing(merged_with_derived, "params.K_P_target"):
        k_p_target = _compute_k_p_target(merged_with_derived)
        if k_p_target is not None:
            derived["params.K_P_target"] = k_p_target

    merged_with_derived = {**merged_with_derived, **derived}
    if _is_missing(merged_with_derived, "params.K_I_target"):
        k_i_target = _compute_k_i_target(merged_with_derived)
        if k_i_target is not None:
            derived["params.K_I_target"] = k_i_target

    controller_with_derived = dict(controller)
    if target in ("sigma", "G"):
        use_sigma = target == "sigma"
        merged_with_derived = {**merged_with_derived, **derived}
        for dst, sigma_key, g_key in TARGET_SWITCH_KEYS:
            active_key = sigma_key if use_sigma else g_key
            inactive_key = g_key if use_sigma else sigma_key

            if dst not in merged_with_derived and active_key in merged_with_derived:
                derived[dst] = merged_with_derived[active_key]

            # Only publish the active target-specific source key.
            controller_with_derived.pop(inactive_key, None)
        controller_with_derived = {**controller_with_derived, **derived}
    else:
        controller_with_derived = {**controller_with_derived, **derived}
    return shared, controller_with_derived, derived
