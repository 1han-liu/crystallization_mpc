"""Translation of gsensor/detection/create_EKF.m."""

import numpy as np


class GrowthRateKalmanFilter:
    def __init__(self, F, H, Q, R, x0, P0=None):
        self.F = np.asarray(F, dtype=float)
        self.H = np.asarray(H, dtype=float)
        self.Q = np.asarray(Q, dtype=float)
        self.R = np.asarray(R, dtype=float)
        self.x = np.asarray(x0, dtype=float).reshape(self.F.shape[0], 1)
        self.P = (
            np.eye(self.F.shape[0], dtype=float)
            if P0 is None
            else np.asarray(P0, dtype=float)
        )

    def predict(self):
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q
        return self.x.reshape(-1)

    def correct(self, measurement):
        z = np.asarray(measurement, dtype=float).reshape(self.H.shape[0], 1)
        innovation = z - self.H @ self.x
        innovation_covariance = self.H @ self.P @ self.H.T + self.R
        kalman_gain = np.linalg.solve(
            innovation_covariance.T,
            (self.P @ self.H.T).T,
        ).T

        self.x = self.x + kalman_gain @ innovation
        identity = np.eye(self.P.shape[0], dtype=float)
        residual_factor = identity - kalman_gain @ self.H
        self.P = (
            residual_factor @ self.P @ residual_factor.T
            + kalman_gain @ self.R @ kalman_gain.T
        )
        return self.x.reshape(-1)


def create_EKF_G(dt_G, resolution, q2, r_diag, x_G):
    dt_G = float(dt_G)
    resolution = float(resolution)
    q2 = float(q2)
    r_diag = np.asarray(r_diag, dtype=float).reshape(-1)

    F = np.array(
        [[1, dt_G, dt_G**2 / 2], [0, 1, dt_G], [0, 0, 1]],
        dtype=float,
    )
    H = np.eye(3, dtype=float)
    Q = np.array(
        [
            [dt_G**4 / 4, dt_G**3 / 2, dt_G**2 / 2],
            [dt_G**3 / 2, dt_G**2, dt_G],
            [dt_G**2 / 2, dt_G, 1],
        ],
        dtype=float,
    ) * q2 * resolution**2
    R = np.diag(r_diag) * resolution**2

    # MATLAB used extendedKalmanFilter here; this model is linear, so the
    # Python translation uses an ordinary Kalman filter with the same state.
    return GrowthRateKalmanFilter(F, H, Q, R, x_G)


__all__ = ["GrowthRateKalmanFilter", "create_EKF_G"]
