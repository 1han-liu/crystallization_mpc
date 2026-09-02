"""Extended Kalman filters with explicit predict/correct state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import numpy as np

from .dynamics import state_transition_function


ArrayFunction = Callable[[np.ndarray], np.ndarray]


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
    steps = np.sqrt(np.finfo(float).eps) * np.maximum(1.0, np.abs(state))
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

    def predict(self) -> np.ndarray:
        prior = self.state.copy()
        F = _forward_jacobian(self.transition, prior)
        self.state = np.asarray(self.transition(prior), dtype=float)
        self.covariance = F @ self.covariance @ F.T + self.process_noise
        return self.state.copy()

    def correct(self, observed: np.ndarray) -> np.ndarray:
        observed_array = np.asarray(observed, dtype=float).reshape(-1)
        H = _forward_jacobian(self.measurement, self.state)
        expected = np.asarray(self.measurement(self.state), dtype=float).reshape(-1)
        innovation_covariance = H @ self.covariance @ H.T + self.measurement_noise
        gain = np.linalg.solve(
            innovation_covariance.T, (self.covariance @ H.T).T
        ).T
        self.state = self.state + gain @ (observed_array - expected)
        self.covariance = (np.eye(self.state.size) - gain @ H) @ self.covariance
        self.covariance = (self.covariance + self.covariance.T) / 2.0
        if not np.isfinite(self.state).all() or not np.isfinite(self.covariance).all():
            raise FloatingPointError("EKF produced non-finite state or covariance.")
        return self.state.copy()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.tolist(),
            "covariance": self.covariance.tolist(),
        }


def construct_EKF(
    state: np.ndarray, params: Mapping[str, Any], dt: float
) -> ExtendedKalmanFilter:
    initial = np.asarray(state, dtype=float).reshape(4)
    return ExtendedKalmanFilter(
        transition=lambda value: state_transition_function(params, value, dt),
        measurement=lambda value: measurement_function(params, value),
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
        transition=lambda value: F @ value,
        measurement=lambda value: value,
        state=np.asarray(state, dtype=float).reshape(3).copy(),
        process_noise=Q,
        measurement_noise=np.diag(np.asarray(r_diag_general, dtype=float)),
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
    return float(filter_.correct(measurement)[0])


def restore_EKF(
    filter_: ExtendedKalmanFilter, state: Mapping[str, Any]
) -> ExtendedKalmanFilter:
    vector = np.asarray(state.get("state"), dtype=float)
    covariance = np.asarray(state.get("covariance"), dtype=float)
    if vector.shape != filter_.state.shape or covariance.shape != filter_.covariance.shape:
        raise ValueError("EKF recovery state has an incompatible shape.")
    if not np.isfinite(vector).all() or not np.isfinite(covariance).all():
        raise ValueError("EKF recovery state must be finite.")
    filter_.state = vector.copy()
    filter_.covariance = covariance.copy()
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
