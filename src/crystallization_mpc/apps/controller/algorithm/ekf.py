"""Extended Kalman filters with explicit predict/correct state."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
from contextlib import contextmanager
from typing import Any, Callable, Mapping

import numpy as np
from ._numeric_backend import cholesky_upper, matmul, qr_r, solve_triangular, svd_right, symmetric_eigenvalues

from crystallization_mpc.apps.controller.tick import ControllerGrowthMeasurementError, ControllerInputValueError
from .._recovery import (
    NumericalRecoveryError, require_finite, validate_covariance,
    validate_covariance_resolution, validate_model_state, validate_state_resolution,
)
from .dynamics import state_transition_function


ArrayFunction = Callable[[np.ndarray], np.ndarray]


def _chol_psd(matrix: np.ndarray) -> np.ndarray:
    """Same positive-definite Cholesky / PSD SVD factor as the source filter."""
    try:
        # The source tests the *upper* Cholesky factor.  On a rank-deficient
        # covariance, lower and upper LAPACK paths can make different rounded
        # definiteness decisions; using the other path changes the later EKF.
        return cholesky_upper(matrix).T
    except np.linalg.LinAlgError:
        singular, right = svd_right(matrix)
        result = matmul(right.conj().T, np.diag(np.sqrt(singular)))
        return result.real if np.iscomplexobj(result) and np.all(result.imag == 0) else result


def _validate_process_noise(value: np.ndarray) -> None:
    """Only Q takes the source constructor's covariance property setter.

    MeasurementNoise deliberately bypasses that setter in this constructor;
    applying these checks to R would invent a source-incompatible restriction.
    """
    def fail(identifier: str, message: str) -> None:
        raise ControllerInputValueError(message, stage="filter_initialization",
                                        reference_identifier=identifier)
    if np.iscomplexobj(value):
        fail("MATLAB:ExtendedKalmanFilter:expectedReal", "ProcessNoise must be real.")
    array = np.asarray(np.real(value), dtype=float)
    if not np.isfinite(array).all():
        fail("MATLAB:ExtendedKalmanFilter:expectedFinite", "ProcessNoise must be finite.")
    if array.ndim != 2 or array.shape[0] != array.shape[1]:
        fail("MATLAB:ExtendedKalmanFilter:expectedSquare", "ProcessNoise must be square.")
    if array.size == 0:
        return
    tolerance = 100.0 * np.spacing(np.max(np.abs(np.diag(array))))
    symmetric = np.all(np.abs(array - array.T) < np.sqrt(tolerance))
    averaged = (array + array.T) / 2.0
    if not np.isfinite(averaged).all():
        fail("MATLAB:eig:matrixWithNaNInf", "Eigenvalue input must not contain NaN or Inf.")
    eigenvalues = symmetric_eigenvalues(averaged)
    if not symmetric or np.any(eigenvalues < -tolerance):
        fail("shared_tracking:KalmanFilter:invalidCovarianceValues",
             "ProcessNoise must be symmetric and positive semidefinite.")


def _qr_factor(matrix: np.ndarray, root: np.ndarray, noise_root: np.ndarray) -> np.ndarray:
    augmented = np.vstack((matmul(root.T, matrix.T), noise_root.T))
    return qr_r(augmented).T


def calc_Q(params: Mapping[str, Any], dt: float) -> np.ndarray:
    Q_T = np.array(
        [[dt**2, dt, 0, 0], [dt, 1, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        dtype=float,
    )
    Q_c = np.array(
        [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, dt**2, dt], [0, 0, dt, 1]],
        dtype=float,
    )
    return Q_T * float(params["var_dT_dt"]) + Q_c * float(params["var_dc_dt"])


def calc_R(params: Mapping[str, Any]) -> np.ndarray:
    return np.diag([float(params["dT"]) ** 2, float(params["dc"]) ** 2])


def measurement_matrices(
    params: Mapping[str, Any], x: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    del params, x
    return (
        np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]),
        np.zeros(2),
    )


def measurement_function(params: Mapping[str, Any], x: np.ndarray) -> np.ndarray:
    state = np.asarray(x, dtype=float).reshape(4)
    C, Du = measurement_matrices(params, state)
    return C @ state + Du


def _forward_jacobian(function: ArrayFunction, state: np.ndarray) -> np.ndarray:
    """Compute a forward finite-difference numerical Jacobian."""

    state = np.asarray(state, dtype=float)
    base = np.asarray(function(state), dtype=float)
    jacobian = np.empty((base.size, state.size), dtype=float)
    delta = np.sqrt(np.finfo(float).eps)
    # Source max(delta,delta*abs(x(j))) omits NaN rather than propagating it.
    steps = np.fmax(delta, delta * np.abs(state))
    for index, step in enumerate(steps):
        shifted = state.copy()
        shifted[index] += step
        jacobian[:, index] = (np.asarray(function(shifted)) - base) / step
    return jacobian


@dataclass
class ExtendedKalmanFilter:
    transition: ArrayFunction
    measurement: ArrayFunction
    state: np.ndarray
    process_noise: np.ndarray
    measurement_noise: np.ndarray
    covariance: np.ndarray
    diagnostic_observer: Callable[[str, Mapping[str, Any]], None] | None = field(
        default=None, repr=False, compare=False
    )
    measurement_jacobian: ArrayFunction | None = field(default=None, repr=False, compare=False)
    state_validator: Callable[[np.ndarray], None] | None = field(default=None, repr=False, compare=False)
    forecast_validator: Callable[[np.ndarray], None] | None = field(default=None, repr=False, compare=False)
    enforce_measurement_resolution: bool = field(default=False, repr=False, compare=False)
    innovation_nis_limit: float | None = field(default=None, repr=False, compare=False)
    last_innovation_nis: float | None = field(default=None, init=False, repr=False, compare=False)
    _forecast_validated_state: np.ndarray | None = field(default=None, init=False, repr=False, compare=False)
    _forecast_validator_used: Callable | None = field(default=None, init=False, repr=False, compare=False)
    _diagnostic_initial_covariance: np.ndarray | None = field(
        default=None, init=False, repr=False, compare=False
    )
    diagnostic_capture_failed: bool = field(default=False, init=False, repr=False, compare=False)
    _sqrt_covariance: np.ndarray | None = field(default=None, init=False, repr=False, compare=False)
    _covariance_snapshot: np.ndarray | None = field(default=None, init=False, repr=False, compare=False)
    _sqrt_process_noise: np.ndarray | None = field(default=None, init=False, repr=False, compare=False)
    _sqrt_measurement_noise: np.ndarray | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # The source extendedKalmanFilter State setter validates real, finite
        # initial state. It does not impose positive temperature/concentration.
        if np.iscomplexobj(self.state):
            raise ControllerInputValueError("Initial filter State must be real.",
                                            stage="filter_initialization",
                                            reference_identifier="MATLAB:ExtendedKalmanFilter:expectedReal")
        if not np.isfinite(self.state).all():
            raise ControllerInputValueError("Initial filter State must be real and finite.",
                                            stage="filter_initialization",
                                            reference_identifier="MATLAB:ExtendedKalmanFilter:expectedFinite")
        validate_covariance(self.covariance, stage="filter_initialization")
        self._sqrt_covariance = _chol_psd(self.covariance)
        self._covariance_snapshot = self.covariance.copy()
        _validate_process_noise(self.process_noise)
        self._sqrt_process_noise = _chol_psd(self.process_noise)
        if np.asarray(self.measurement_noise).size == 0:
            self.measurement_noise = np.ones((1, 1))
        if np.asarray(self.measurement_noise).ndim != 2 or self.measurement_noise.shape[0] != self.measurement_noise.shape[1]:
            raise ControllerInputValueError("Matrix must be square.", stage="filter_initialization",
                                            reference_identifier="MATLAB:square")
        validate_covariance(self.measurement_noise, stage="filter_initialization")
        self._sqrt_measurement_noise = _chol_psd(self.measurement_noise)
        self.measurement_noise = matmul(self._sqrt_measurement_noise, self._sqrt_measurement_noise.conj().T)
        self._validate_candidate(self.state, self.covariance, stage="filter_initialization")
        # Observational only: constructor P remains unchanged and this copy is
        # deliberately absent from the recovery wire format.
        try:
            self._diagnostic_initial_covariance = self.covariance.copy()
        except Exception:
            self._diagnostic_initial_covariance = None

    def _observe(self, stage: str, **values: Any) -> None:
        try:
            if self.diagnostic_observer is not None:
                self.diagnostic_observer(stage, {
                    key: value.copy() if isinstance(value, np.ndarray) else value
                    for key, value in values.items()
                })
        except Exception:
            # A chart must never turn a valid numerical cycle into a failure.
            self.diagnostic_capture_failed = True

    def _synchronize_covariance(self) -> None:
        if (self._sqrt_covariance is None or self._covariance_snapshot is None
                or not np.array_equal(self.covariance, self._covariance_snapshot, equal_nan=True)):
            self._sqrt_covariance = _chol_psd(self.covariance)
            self._covariance_snapshot = self.covariance.copy()

    def restore_covariance(self, covariance: np.ndarray, root: np.ndarray | None = None) -> None:
        candidate = covariance.copy()
        self._validate_candidate(self.state, candidate, stage="filter_restore_covariance")
        factor = _chol_psd(candidate) if root is None else root.copy()
        require_finite(factor, "restored covariance root", stage="filter_restore")
        if factor.shape != candidate.shape:
            raise ValueError("Covariance root has an incompatible shape.")
        reconstructed = matmul(factor, factor.T)
        scales = np.sqrt(np.maximum(np.diag(candidate), np.finfo(float).tiny))
        tolerance = 64.0 * candidate.shape[0] * np.finfo(float).eps
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            discrepancy = (reconstructed - candidate) / scales[:, None] / scales[None, :]
        if not np.isfinite(discrepancy).all() or np.max(np.abs(discrepancy)) > tolerance:
            raise NumericalRecoveryError("Covariance root does not match its covariance.", module="main", stage="filter_restore")
        self.covariance = candidate
        self._sqrt_covariance = factor
        self._covariance_snapshot = candidate.copy()

    def _validate_candidate(self, state: np.ndarray, covariance: np.ndarray, *, stage: str) -> None:
        require_finite(state, "filter state", stage=stage)
        if np.asarray(state).shape != self.state.shape:
            raise NumericalRecoveryError("Filter state shape changed.", module="main", stage=stage)
        validate_covariance(covariance, stage=stage)
        if self.state_validator is not None:
            self.state_validator(state)
        if self.enforce_measurement_resolution:
            validate_covariance_resolution(covariance, self.measurement_noise, stage=stage)
        if stage not in {"filter_predict", "filter_correct_input", "filter_restore_covariance"}:
            self._validate_forecast(state)

    def _validate_forecast(self, state: np.ndarray) -> None:
        if self.forecast_validator is None:
            return
        # A pure validation cache is scoped to the captured params/dt callback.
        # Rebinding either creates a new callback; no rejected result is cached.
        if (self._forecast_validator_used is not self.forecast_validator
                or self._forecast_validated_state is None
                or not np.array_equal(state, self._forecast_validated_state)):
            self.forecast_validator(state)
            self._forecast_validated_state = state.copy()
            self._forecast_validator_used = self.forecast_validator

    @contextmanager
    def _transaction(self):
        snapshot = self.numerical_snapshot()
        try:
            yield
        except Exception:
            self.restore_numerical_snapshot(snapshot)
            raise

    def numerical_snapshot(self) -> dict[str, Any]:
        """Copy computational memory without a constructor or factorization."""
        names = ("state", "covariance", "process_noise", "measurement_noise",
                 "_sqrt_covariance", "_covariance_snapshot", "_sqrt_process_noise",
                 "_sqrt_measurement_noise", "_diagnostic_initial_covariance", "last_innovation_nis",
                 "measurement_jacobian", "state_validator", "enforce_measurement_resolution",
                 "innovation_nis_limit", "forecast_validator", "_forecast_validated_state",
                 "_forecast_validator_used")
        return {**{name: copy.deepcopy(getattr(self, name)) for name in names},
                "transition": self.transition, "measurement": self.measurement}

    def restore_numerical_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Restore only an interrupted module; retain the filter identity."""
        for name, value in snapshot.items():
            setattr(self, name, value if name in {"transition", "measurement"} else copy.deepcopy(value))

    def predict(self) -> np.ndarray:
        with self._transaction():
            self._validate_candidate(self.state, self.covariance, stage="filter_predict")
            self._synchronize_covariance()
            prior = self.state.copy()
            F = _forward_jacobian(self.transition, prior)
            require_finite(F, "transition Jacobian", stage="filter_predict")
            predicted = np.asarray(self.transition(prior))
            require_finite(predicted, "predicted state", stage="filter_predict")
            predicted_root = _qr_factor(F, self._sqrt_covariance, self._sqrt_process_noise)
            predicted_covariance = matmul(predicted_root, predicted_root.T)
            self._validate_candidate(predicted, predicted_covariance, stage="filter_predict")
            self.state = predicted
            self.covariance = predicted_covariance
            self._sqrt_covariance = predicted_root
            self._covariance_snapshot = predicted_covariance.copy()
            self._observe("prior", state=self.state, covariance=self.covariance)
            return self.state.copy()

    def correct(self, observed: np.ndarray) -> np.ndarray:
        if np.iscomplexobj(observed) or not np.isfinite(np.asarray(observed)).all():
            raise ControllerInputValueError("Filter correction measurement z must be real and finite.",
                                            reference_identifier="MATLAB:ExtendedKalmanFilter:expectedFinite")
        with self._transaction():
            self._validate_candidate(self.state, self.covariance, stage="filter_correct_input")
            self._synchronize_covariance()
            observed_array = np.asarray(observed, dtype=float).reshape(-1)
            H = (np.asarray(self.measurement_jacobian(self.state)) if self.measurement_jacobian is not None
                 else _forward_jacobian(self.measurement, self.state))
            expected = np.asarray(self.measurement(self.state), dtype=float).reshape(-1)
            require_finite(H, "measurement Jacobian", stage="filter_correct")
            require_finite(expected, "expected measurement", stage="filter_correct")
            if H.shape != (observed_array.size, self.state.size) or expected.size != observed_array.size:
                raise ControllerInputValueError("Measurement has incompatible dimensions.")
            noise_root = self._sqrt_measurement_noise
            if noise_root.shape == (1, 1) and observed_array.size != 1:
                noise_root = np.eye(observed_array.size) * noise_root[0, 0]
                self._sqrt_measurement_noise = noise_root
                self.measurement_noise = matmul(noise_root, noise_root.conj().T)
            elif noise_root.shape[0] != observed_array.size:
                raise ControllerInputValueError("MeasurementNoise has incompatible dimensions.",
                                                reference_identifier="shared_tracking:KalmanFilter:invalidCovarianceDims")
            innovation_root = _qr_factor(H, self._sqrt_covariance, noise_root)
            innovation = observed_array - expected
            require_finite(innovation, "measurement innovation", stage="filter_correct")
            whitened = solve_triangular(innovation_root, innovation, lower=True, check_finite=False)
            nis = float(np.dot(whitened, whitened))
            require_finite(nis, "normalized innovation", stage="filter_correct")
            # No statistical cut-off is invented here: a configured limit must
            # come from the calibrated observation policy and its false-alarm budget.
            if self.innovation_nis_limit is not None:
                if not np.isfinite(self.innovation_nis_limit) or self.innovation_nis_limit <= 0:
                    raise ValueError("Configured innovation limit must be finite and positive.")
                if nis > self.innovation_nis_limit:
                    raise NumericalRecoveryError("Innovation exceeds the configured normalized limit.",
                                                 module="main", stage="filter_correct")
            cross_covariance = matmul(matmul(self._sqrt_covariance, self._sqrt_covariance.T), H.T)
            gain = solve_triangular(innovation_root.T,
                                    solve_triangular(innovation_root, cross_covariance.T, lower=True, check_finite=False),
                                    lower=False, check_finite=False).T
            require_finite(gain, "Kalman gain", stage="filter_correct")
            corrected = self.state + matmul(gain, innovation)
            correction = matmul(-gain, H)
            for index in range(self.state.size):
                correction[index, index] += 1.0
            corrected_root = _qr_factor(correction, self._sqrt_covariance, matmul(gain, noise_root))
            corrected_covariance = matmul(corrected_root, corrected_root.T)
            self._validate_candidate(corrected, corrected_covariance, stage="filter_correct")
            self.state = corrected
            self._sqrt_covariance = corrected_root
            self.covariance = corrected_covariance
            self._covariance_snapshot = corrected_covariance.copy()
            self.last_innovation_nis = nis
            innovation_covariance = matmul(innovation_root, innovation_root.T)
            self._observe(
                "posterior", state=self.state, covariance=self.covariance,
                innovation=innovation, innovation_covariance=innovation_covariance,
                normalized_innovation_squared=nis,
            )
            return self.state.copy()

    def to_dict(self) -> dict[str, Any]:
        self._validate_candidate(self.state, self.covariance, stage="filter_checkpoint")
        self._synchronize_covariance()
        return {
            "state": self.state.tolist(),
            "covariance": self.covariance.tolist(),
            "sqrt_covariance": self._sqrt_covariance.tolist(),
        }


def construct_EKF(
    state: np.ndarray, params: Mapping[str, Any], dt: float
) -> ExtendedKalmanFilter:
    initial = np.asarray(state).reshape(4)
    transition, measurement, jacobian, validator, forecast = _main_filter_functions(params, dt, calc_R(params))
    return ExtendedKalmanFilter(
        transition=transition,
        measurement=measurement,
        state=initial.copy(),
        process_noise=calc_Q(params, dt),
        measurement_noise=calc_R(params),
        covariance=np.eye(4),
        measurement_jacobian=jacobian,
        state_validator=validator,
        forecast_validator=forecast,
        enforce_measurement_resolution=True,
    )


def _main_filter_functions(params: Mapping[str, Any], dt: float, noise: np.ndarray):
    captured_params = copy.deepcopy(dict(params))
    captured_dt = float(dt)
    if not np.isfinite(captured_dt) or captured_dt <= 0:
        raise ValueError("Main-filter interval must be finite and positive.")
    captured_noise = np.asarray(noise).copy()

    def validator(value: np.ndarray) -> None:
        validate_model_state(value, stage="main_filter_state")
        validate_state_resolution(value, captured_noise, captured_dt, stage="main_filter_state")

    def forecast(value: np.ndarray) -> None:
        predicted = state_transition_function(captured_params, value, captured_dt)
        validate_state_resolution(predicted, captured_noise, 0.0, stage="main_filter_forecast")

    return (
        lambda value: state_transition_function(captured_params, value, captured_dt),
        lambda value: measurement_function(captured_params, value),
        lambda value: measurement_matrices(captured_params, value)[0],
        validator,
        forecast,
    )


def update_EKF_interval(filter_: ExtendedKalmanFilter, params: Mapping[str, Any], dt: float) -> None:
    """Rebind the main prediction clock/Q without resetting its state or P.

    Call under the controller's lock after the process-time admission policy.
    The same entry point attaches main-filter guards after neutral restoration.
    """
    transition, measurement, jacobian, validator, forecast = _main_filter_functions(params, dt, filter_.measurement_noise)
    noise = calc_Q(params, float(dt))
    _validate_process_noise(noise)
    noise_root = _chol_psd(noise)
    validator(filter_.state)
    validate_covariance(filter_.covariance, stage="main_filter_bind")
    validate_covariance_resolution(filter_.covariance, filter_.measurement_noise, stage="main_filter_bind")
    forecast(filter_.state)
    with filter_._transaction():
        filter_.transition = transition
        filter_.measurement = measurement
        filter_.measurement_jacobian = jacobian
        filter_.state_validator = validator
        filter_.forecast_validator = forecast
        filter_._forecast_validated_state = filter_.state.copy()
        filter_._forecast_validator_used = forecast
        filter_.enforce_measurement_resolution = True
        filter_.process_noise = noise
        filter_._sqrt_process_noise = noise_root


def smooth_EKF(
    filter_: ExtendedKalmanFilter,
    params: Mapping[str, Any],
    measurement_state: np.ndarray,
) -> np.ndarray:
    with filter_._transaction():
        filter_.predict()
        return filter_.correct(measurement_function(params, measurement_state))


def create_EKF_general(
    dt: float,
    q2_general: float,
    r_diag_general: list[float] | tuple[float, ...] | np.ndarray,
    state: np.ndarray,
) -> ExtendedKalmanFilter:
    F = np.array(
        [[1.0, dt, dt**2 / 2.0], [0.0, 1.0, dt], [0.0, 0.0, 1.0]],
        dtype=float,
    )
    Q = np.array(
        [
            [dt**4 / 4.0, dt**3 / 2.0, dt**2 / 2.0],
            [dt**3 / 2.0, dt**2, dt],
            [dt**2 / 2.0, dt, 1.0],
        ],
        dtype=float,
    ) * float(q2_general)
    return ExtendedKalmanFilter(
        transition=lambda value: matmul(F, value),
        measurement=lambda value: value,
        state=np.asarray(state).reshape(3).copy(),
        process_noise=Q,
        measurement_noise=np.diag(np.asarray(r_diag_general)),
        covariance=np.eye(3),
    )


def smooth_EKF_general(
    values: list[float] | np.ndarray,
    filter_: ExtendedKalmanFilter,
    dt: float,
) -> float:
    array = np.asarray(values, dtype=float).reshape(-1)
    if array.size == 0:
        raise ValueError("General EKF requires at least one value.")
    filter_.predict()
    measurement = np.array([array[-1], 0.0, 0.0])
    if array.size > 1:
        measurement[1] = (array[-1] - array[-2]) / float(dt)
    if array.size > 2:
        measurement[2] = (array[-1] + array[-3] - 2.0 * array[-2]) / float(dt) ** 2
    try:
        return float(filter_.correct(measurement)[0])
    except ControllerInputValueError as exc:
        raise ControllerGrowthMeasurementError(str(exc)) from exc


def restore_EKF(
    filter_: ExtendedKalmanFilter, state: Mapping[str, Any]
) -> ExtendedKalmanFilter:
    vector = np.asarray(state.get("state"), dtype=float)
    covariance = np.asarray(state.get("covariance"), dtype=float)
    if vector.shape != filter_.state.shape or covariance.shape != filter_.covariance.shape:
        raise ValueError("EKF recovery state has an incompatible shape.")
    with filter_._transaction():
        filter_._validate_candidate(vector, covariance, stage="filter_restore")
        filter_.state = vector.copy()
        root = state.get("sqrt_covariance")
        filter_.restore_covariance(covariance, np.asarray(root, dtype=float) if root is not None else None)
    return filter_


__all__ = [
    "ExtendedKalmanFilter",
    "calc_Q",
    "calc_R",
    "construct_EKF",
    "create_EKF_general",
    "measurement_function",
    "measurement_matrices",
    "restore_EKF",
    "update_EKF_interval",
    "smooth_EKF",
    "smooth_EKF_general",
]
