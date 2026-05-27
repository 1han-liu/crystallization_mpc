import numpy as np
import pytest

import crystallization_mpc.apps.gsensor.morphs.recover_3d as recover_3d_module


def _calc_angle_d(vector_1, vector_2):
    vector_1 = np.asarray(vector_1, dtype=float)
    vector_2 = np.asarray(vector_2, dtype=float)
    cosine = np.dot(vector_1, vector_2) / (
        np.linalg.norm(vector_1) * np.linalg.norm(vector_2)
    )
    return float(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0))))


def test_recover_3d_recovers_w_u_v_z_values(monkeypatch):
    m = np.array([0.0, 0.0])
    w = np.array([10.0, 0.0])
    u = np.array([0.0, 20.0])
    v = np.array([30.0, 10.0])

    expected_m = np.array([0.0, 0.0, 0.0])
    expected_w = np.array([10.0, 0.0, 40.0])
    expected_u = np.array([0.0, 20.0, 70.0])
    expected_v = np.array([30.0, 10.0, 100.0])

    def get_angles_d(corner):
        assert corner == "A"
        return (
            _calc_angle_d(expected_w - expected_m, expected_u - expected_m),
            _calc_angle_d(expected_w - expected_m, expected_v - expected_m),
            _calc_angle_d(expected_u - expected_m, expected_v - expected_m),
        )

    monkeypatch.setattr(recover_3d_module, "get_angles_d", get_angles_d)
    monkeypatch.setattr(recover_3d_module, "calc_angle_d", _calc_angle_d)

    M, W, U, V = recover_3d_module.recover_3d(
        m,
        w,
        u,
        v,
        "A",
        True,
        "outwards",
    )

    assert M == pytest.approx(expected_m)
    assert W == pytest.approx(expected_w, abs=1.0e-3)
    assert U == pytest.approx(expected_u, abs=1.0e-3)
    assert V == pytest.approx(expected_v, abs=1.0e-3)
