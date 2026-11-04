"""Controller-local numerical availability checks, without input substitution.

These are recovery policy checks, deliberately separate from the reference
equations. No sensor calibration limits or relative-jump thresholds are invented.
"""
from __future__ import annotations

from typing import Any
import numpy as np

from .tick import ControllerInputValueError


class NumericalRecoveryError(ControllerInputValueError):
    def __init__(self, message: str, *, module: str, stage: str):
        self.module = module
        super().__init__(message, stage=stage)


def finite_scalar(value: Any, name: str, *, module: str = "main", stage: str = "input") -> float:
    try:
        array = np.asarray(value)
        if array.size != 1 or np.iscomplexobj(array) or array.dtype.kind not in "iuf":
            raise ValueError("not a real numeric scalar")
        result = float(array.reshape(-1)[0])
        if not np.isfinite(result):
            raise ValueError("not finite")
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise NumericalRecoveryError(f"{name} is unavailable for numerical calculation: {exc}",
                                     module=module, stage=stage) from exc


def require_finite(value: Any, name: str, *, module: str = "main", stage: str = "calculation") -> None:
    try:
        array = np.asarray(value)
        if np.iscomplexobj(array) or not np.isfinite(array).all():
            raise ValueError("contains a complex or nonfinite value")
    except (TypeError, ValueError, OverflowError) as exc:
        raise NumericalRecoveryError(f"{name}: {exc}", module=module, stage=stage) from exc


def validate_measurement(name: str, value: Any, *, stage: str = "process_measurement") -> None:
    module = "main" if name in {"T", "c"} else "input"
    scalar = finite_scalar(value, name, module=module, stage=stage)
    if name in {"T", "T_j", "T_j_set"} and scalar <= 0:
        raise NumericalRecoveryError(f"{name} is outside the positive-Kelvin model domain.",
                                     module=module, stage=stage)
    if name == "count_middle" and scalar < 0:
        raise NumericalRecoveryError("Negative particle count is unavailable for this controller.",
                                     module="input", stage=stage)
    if name == "c" and scalar <= 0:
        # Zero is retained as an actual measurement, not called a sensor error.
        # The current seeded-growth state model has no zero-solute operating
        # branch. Admission avoids its demonstrated delayed derivative blow-up.
        raise NumericalRecoveryError("Nonpositive concentration is unavailable to the seeded-growth model.",
                                     module="main", stage=stage)


def validate_model_state(state: Any, *, stage: str) -> None:
    require_finite(state, "main model state", stage=stage)
    values = np.asarray(state).reshape(4)
    validate_measurement("T", values[0], stage=stage)
    validate_measurement("c", values[2], stage=stage)
    # The solubility and metastable-limit polynomials must describe an ordered
    # positive interval. This is a mathematical model-domain check, not jacket
    # limits repurposed as reactor sensor limits.
    from .algorithm.thermodynamics import calc_c_meta, calc_c_sat
    with np.errstate(over="ignore", invalid="ignore"):
        saturation, metastable = calc_c_sat(values[0]), calc_c_meta(values[0])
    require_finite([saturation, metastable], "solubility model interval", stage=stage)
    if saturation <= 0 or metastable < saturation:
        raise NumericalRecoveryError("Temperature is outside the ordered solubility-model interval.",
                                     module="main", stage=stage)


def validate_filter(filter_: Any, *, module: str, stage: str) -> None:
    if filter_ is None:
        raise NumericalRecoveryError("Filter is not initialized.", module=module, stage=stage)
    for name in ("state", "covariance", "_sqrt_covariance", "process_noise", "measurement_noise"):
        value = getattr(filter_, name)
        if value is not None:
            require_finite(value, f"{module}.{name}", module=module, stage=stage)
    if np.any(np.diag(filter_.covariance) < 0):
        raise NumericalRecoveryError("Negative covariance diagonal.", module=module, stage=stage)
    if module == "main":
        validate_model_state(filter_.state, stage=stage)
