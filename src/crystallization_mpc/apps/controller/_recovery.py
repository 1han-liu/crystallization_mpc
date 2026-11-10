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


def validate_covariance(matrix: Any, *, stage: str) -> None:
    """Check covariance geometry after scaling out its physical units.

    The tolerance is a floating-point backward-error allowance for small dense
    factorizations, not a temperature/concentration or equipment threshold.
    """
    require_finite(matrix, "filter covariance", stage=stage)
    value = np.asarray(matrix, dtype=float)
    if value.ndim != 2 or value.shape[0] != value.shape[1] or not value.size:
        raise NumericalRecoveryError("Covariance must be a nonempty square matrix.", module="main", stage=stage)
    if np.any(np.diag(value) < 0):
        raise NumericalRecoveryError("Negative covariance diagonal.", module="main", stage=stage)
    scales = np.sqrt(np.maximum(np.diag(value), np.finfo(float).tiny))
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        normalized = value / scales[:, None] / scales[None, :]
        norm = np.linalg.norm(normalized, ord=np.inf)
    require_finite([norm], "scaled covariance", stage=stage)
    tolerance = 64.0 * value.shape[0] * np.finfo(float).eps * max(1.0, float(norm))
    if np.max(np.abs(normalized - normalized.T)) > tolerance:
        raise NumericalRecoveryError("Covariance is not numerically symmetric.", module="main", stage=stage)
    try:
        eigenvalues = np.linalg.eigvalsh((normalized + normalized.T) / 2.0)
    except np.linalg.LinAlgError as exc:
        raise NumericalRecoveryError("Covariance factorization is unavailable.", module="main", stage=stage) from exc
    if not np.isfinite(eigenvalues).all() or np.min(eigenvalues) < -tolerance:
        raise NumericalRecoveryError("Covariance is not positive semidefinite.", module="main", stage=stage)


def validate_state_resolution(state: Any, noise: Any, dt: float, *, stage: str) -> None:
    """Reject states that cannot resolve the configured measurement accuracy.

    This uses IEEE double spacing and configured R, including the next nominal
    prediction interval. It deliberately invents no physical IR upper limit or
    fixed allowable concentration jump; those belong to calibrated input policy.
    """
    values = np.asarray(state, dtype=float).reshape(4)
    variance = np.asarray(noise, dtype=float)
    observed = values[[0, 2]]
    with np.errstate(over="ignore", invalid="ignore"):
        projected = observed + float(dt) * values[[1, 3]]
    require_finite(projected, "measurement-resolution horizon", stage=stage)
    accuracy = np.sqrt(np.diag(variance))
    for vector in (observed, projected):
        with np.errstate(over="ignore", invalid="ignore"):
            spacing = np.abs(np.spacing(np.abs(vector)))
        if np.any((accuracy > 0) & (spacing > accuracy)):
            raise NumericalRecoveryError(
                "Main filter state cannot resolve the configured measurement precision.",
                module="main", stage=stage,
            )


def validate_covariance_resolution(covariance: Any, noise: Any, *, stage: str) -> None:
    """Require main-channel uncertainty to retain the configured R in double precision."""
    diagonal = np.diag(np.asarray(covariance, dtype=float))[[0, 2]]
    variance = np.diag(np.asarray(noise, dtype=float))
    with np.errstate(over="ignore", invalid="ignore"):
        spacing = np.abs(np.spacing(np.abs(diagonal)))
    if np.any((variance > 0) & (spacing > variance)):
        raise NumericalRecoveryError(
            "Main filter covariance cannot resolve the configured measurement noise.",
            module="main", stage=stage,
        )


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
    validate_covariance(filter_.covariance, stage=stage)
    validator = getattr(filter_, "state_validator", None)
    if callable(validator):
        validator(filter_.state)
    if getattr(filter_, "enforce_measurement_resolution", False):
        validate_covariance_resolution(filter_.covariance, filter_.measurement_noise, stage=stage)
        forecast = getattr(filter_, "_validate_forecast", None)
        if callable(forecast):
            forecast(filter_.state)
