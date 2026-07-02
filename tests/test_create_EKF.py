import numpy as np

from crystallization_mpc.apps.gsensor.detection.create_EKF import create_EKF_G


def test_create_EKF_G_predicts_constant_acceleration_state():
    ekf = create_EKF_G(
        dt_G=2.0,
        resolution=0.5,
        q2=0.1,
        r_diag=[1.0, 2.0, 3.0],
        x_G=[[1.0], [2.0], [3.0]],
    )

    predicted = ekf.predict()

    np.testing.assert_allclose(predicted, [11.0, 8.0, 3.0])
    np.testing.assert_allclose(
        ekf.Q,
        np.array([[4.0, 4.0, 2.0], [4.0, 4.0, 2.0], [2.0, 2.0, 1.0]])
        * 0.1
        * 0.5**2,
    )
    np.testing.assert_allclose(ekf.R, np.diag([1.0, 2.0, 3.0]) * 0.5**2)


def test_create_EKF_G_corrects_with_measurement():
    ekf = create_EKF_G(
        dt_G=1.0,
        resolution=1.0,
        q2=0.0,
        r_diag=[1.0, 1.0, 1.0],
        x_G=[[0.0], [0.0], [0.0]],
    )

    ekf.predict()
    corrected = ekf.correct([10.0, 0.0, 0.0])

    assert corrected.shape == (3,)
    assert 0.0 < corrected[0] < 10.0
    assert np.all(np.isfinite(corrected))
