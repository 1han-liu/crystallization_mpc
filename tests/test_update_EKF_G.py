from types import SimpleNamespace

import numpy as np

from crystallization_mpc.apps.gsensor.detection.update_EKF_G import update_EKF_G


class FakeEKF:
    def __init__(self, correction):
        self.predicted = False
        self.measurements = []
        self.correction = np.asarray(correction, dtype=float)

    def predict(self):
        self.predicted = True
        return self.correction

    def correct(self, measurement):
        self.measurements.append(np.asarray(measurement, dtype=float).copy())
        return self.correction


def test_update_EKF_G_first_frame_uses_distance_only():
    ekf = FakeEKF([10.0, 20.0, 30.0])
    uv_struct = SimpleNamespace(
        EKF_G=ekf,
        dist_array=[5.0],
        distance_array=[],
        G_array=[],
        x_G_array=[],
        distance_KF_array=[],
        G_KF_array=[],
    )

    result = update_EKF_G(uv_struct, dt_G=2.0, resolution=0.5, ii=1)

    assert result is uv_struct
    assert ekf.predicted is True
    np.testing.assert_allclose(ekf.measurements[0], [2.5, 0.0, 0.0])
    assert uv_struct.distance_array == [2.5]
    assert uv_struct.G_array == [0.0]
    np.testing.assert_allclose(uv_struct.x_G_array[:, 0], [10.0, 20.0, 30.0])
    assert uv_struct.distance_KF_array == [10.0]
    assert uv_struct.G_KF_array == [20.0]


def test_update_EKF_G_third_frame_uses_velocity_and_acceleration():
    ekf = FakeEKF([3.0, 4.0, 5.0])
    uv_struct = SimpleNamespace(
        EKF_G=ekf,
        dist_array=[10.0, 14.0, 23.0],
        distance_array=[],
        G_array=[],
        x_G_array=[],
        distance_KF_array=[],
        G_KF_array=[],
    )

    update_EKF_G(uv_struct, dt_G=3.0, resolution=2.0, ii=3)

    np.testing.assert_allclose(ekf.measurements[0], [46.0, 6.0, 10.0 / 9.0])
    assert uv_struct.distance_array == [None, None, 46.0]
    assert uv_struct.G_array == [None, None, 6.0]
    np.testing.assert_allclose(uv_struct.x_G_array[:, 2], [3.0, 4.0, 5.0])
    assert uv_struct.distance_KF_array == [None, None, 3.0]
    assert uv_struct.G_KF_array == [None, None, 4.0]
