"""Translation of gsensor/detection/create_EKF.m."""

import numpy as np


def extendedKalmanFilter(*args, **kwargs):
    raise NotImplementedError("Placeholder for MATLAB extendedKalmanFilter.")


def create_EKF_G(dt_G, resolution, q2, r_diag, x_G):
    F_func = lambda x_G_, dt: np.array(
        [[1, dt, dt**2 / 2], [0, 1, dt], [0, 0, 1]]
    ) @ x_G_
    y_func = lambda x_G_: x_G_
    Q_func = lambda q2, dt: np.array(
        [
            [dt**4 / 4, dt**3 / 2, dt**2 / 2],
            [dt**3 / 2, dt**2, dt],
            [dt**2 / 2, dt, 1],
        ]
    ) * q2 * resolution**2
    R = np.diag(r_diag) * resolution**2

    EKF_G = extendedKalmanFilter(
        lambda x_G_: F_func(x_G_, dt_G),
        lambda x_G_: y_func(x_G_),
        x_G,
        ProcessNoise=Q_func(q2, dt_G),
        MeasurementNoise=R,
        StateCovariance=[],
    )
    return EKF_G
