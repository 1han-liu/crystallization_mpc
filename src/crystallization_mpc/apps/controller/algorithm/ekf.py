"""Extended Kalman filters with explicit predict/correct state."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
from typing import Any, Callable, Mapping

import numpy as np
from ._numeric_backend import cholesky_upper, matmul, qr_r, solve_triangular, svd_right, symmetric_eigenvalues

from crystallization_mpc.apps.controller.tick import ControllerGrowthMeasurementError, ControllerInputValueError
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
        self._sqrt_covariance = _chol_psd(self.covariance)
        self._covariance_snapshot = self.covariance.copy()
        _validate_process_noise(self.process_noise)
        self._sqrt_process_noise = _chol_psd(self.process_noise)
        if np.asarray(self.measurement_noise).size == 0:
            self.measurement_noise = np.ones((1, 1))
        if np.asarray(self.measurement_noise).ndim != 2 or self.measurement_noise.shape[0] != self.measurement_noise.shape[1]:
            raise ControllerInputValueError("Matrix must be square.", stage="filter_initialization",
                                            reference_identifier="MATLAB:square")
        self._sqrt_measurement_noise = _chol_psd(self.measurement_noise)
        self.measurement_noise = matmul(self._sqrt_measurement_noise, self._sqrt_measurement_noise.conj().T)
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
        self.covariance = covariance.copy()
        self._sqrt_covariance = _chol_psd(covariance) if root is None else root.copy()
        self._covariance_snapshot = covariance.copy()

    def predict(self) -> np.ndarray:
        self._synchronize_covariance()
        prior = self.state.copy()
        F = _forward_jacobian(self.transition, prior)
        predicted = np.asarray(self.transition(prior), dtype=float)
        predicted_root = _qr_factor(F, self._sqrt_covariance, self._sqrt_process_noise)
        predicted_covariance = matmul(predicted_root, predicted_root.T)
        self.state = predicted
        self.covariance = predicted_covariance
        self._sqrt_covariance = predicted_root
        self._covariance_snapshot = predicted_covariance.copy()
        self._observe("prior", state=self.state, covariance=self.covariance)
        return self.state.copy()

    def correct(self, observed: np.ndarray) -> np.ndarray:
        # Source correct(z) checks z before its internal correction can mutate
        # pState/P. A preceding predict is deliberately not undone on failure.
        if np.iscomplexobj(observed) or not np.isfinite(np.asarray(observed)).all():
            raise ControllerInputValueError("Filter correction measurement z must be real and finite.",
                                            reference_identifier="MATLAB:ExtendedKalmanFilter:expectedFinite")
        self._synchronize_covariance()
        observed_array = np.asarray(observed, dtype=float).reshape(-1)
        H = _forward_jacobian(self.measurement, self.state)
        expected = np.asarray(self.measurement(self.state), dtype=float).reshape(-1)
        noise_root = self._sqrt_measurement_noise
        if noise_root.shape == (1, 1) and observed_array.size != 1:
            noise_root = np.eye(observed_array.size) * noise_root[0, 0]
            self._sqrt_measurement_noise = noise_root
            self.measurement_noise = matmul(noise_root, noise_root.conj().T)
        elif noise_root.shape[0] != observed_array.size:
            raise ControllerInputValueError("MeasurementNoise has incompatible dimensions.",
                                            reference_identifier="shared_tracking:KalmanFilter:invalidCovarianceDims")
        innovation_root = _qr_factor(H, self._sqrt_covariance, noise_root)
        cross_covariance = matmul(matmul(self._sqrt_covariance, self._sqrt_covariance.T), H.T)
        gain = solve_triangular(innovation_root.T,
                                solve_triangular(innovation_root, cross_covariance.T, lower=True, check_finite=False),
                                lower=False, check_finite=False).T
        corrected = self.state + matmul(gain, observed_array - expected)
        correction = matmul(-gain, H)
        for index in range(self.state.size):
            correction[index, index] += 1.0
        corrected_root = _qr_factor(correction, self._sqrt_covariance, matmul(gain, noise_root))
        self.state = corrected
        self._sqrt_covariance = corrected_root
        self.covariance = matmul(corrected_root, corrected_root.T)
        self._covariance_snapshot = self.covariance.copy()
        innovation_covariance = matmul(innovation_root, innovation_root.T)
        self._observe(
            "posterior", state=self.state, covariance=self.covariance,
            innovation=observed_array - expected,
            innovation_covariance=innovation_covariance,
        )
        return self.state.copy()

    def to_dict(self) -> dict[str, Any]:
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
    captured_params = copy.deepcopy(params)
    captured_dt = float(dt)
    return ExtendedKalmanFilter(
        transition=lambda value: state_transition_function(captured_params, value, captured_dt),
        measurement=lambda value: measurement_function(captured_params, value),
        state=initial.copy(),
        process_noise=calc_Q(params, dt),
        measurement_noise=calc_R(params),
        covariance=np.eye(4),
    )


def smooth_EKF(
    filter_: ExtendedKalmanFilter,
    params: Mapping[str, Any],
    measurement_state: np.ndarray,
) -> np.ndarray:
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
    # This restores the saved internal object fields, like MATLAB loadobj;
    # it is not a new State-property constructor assignment. The checkpoint
    # validator supplies the corresponding recorded computation provenance.
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
    "smooth_EKF",
    "smooth_EKF_general",
]
