from types import SimpleNamespace

import numpy as np

from crystallization_mpc.apps.gsensor.morphs.calc_2d_3d_info import calc_2d_3d_info


def _uv_struct(n, t=(2.0, 0.0, 0.0), o=(1.0, 1.0, 0.0)):
    return SimpleNamespace(
        n=np.asarray(n, dtype=float),
        t=np.asarray(t, dtype=float),
        o=np.asarray(o, dtype=float),
    )


def test_calc_2d_3d_info_uses_opposite_structs_for_full_mode():
    M = np.array([0.0, 0.0, 0.0])
    W = np.array([1.0, 0.0, 0.0])
    U = np.array([0.0, 1.0, 0.0])
    V = np.array([0.0, 0.0, 1.0])
    u_op = _uv_struct([0.0, 0.0, 1.0])
    v_op = _uv_struct([0.0, -1.0, 0.0])
    u_ad = _uv_struct([1.0, 0.0, 0.0])
    v_ad = _uv_struct([1.0, 0.0, 0.0])

    u_struct, v_struct = calc_2d_3d_info(M, W, U, V, u_op, v_op, u_ad, v_ad, True)

    assert u_struct is u_op
    assert v_struct is v_op
    np.testing.assert_allclose(u_struct.n_3d, [0.0, 0.0, 1.0])
    np.testing.assert_allclose(v_struct.n_3d, [0.0, -1.0, 0.0])
    assert u_struct.cos == 1.0
    assert v_struct.cos == 1.0
    assert u_struct.rho_0 == 0.0
    assert v_struct.rho_0 == 0.0
    assert bool(u_struct.is_opposite) is False


def test_calc_2d_3d_info_uses_adjacent_structs_for_non_full_mode():
    M = np.array([0.0, 0.0, 0.0])
    W = np.array([1.0, 0.0, 0.0])
    U = np.array([0.0, 1.0, 0.0])
    V = np.array([0.0, 0.0, 1.0])
    u_op = _uv_struct([1.0, 0.0, 0.0])
    v_op = _uv_struct([1.0, 0.0, 0.0])
    u_ad = _uv_struct([0.0, 0.0, -1.0])
    v_ad = _uv_struct([0.0, 1.0, 0.0])

    u_struct, v_struct = calc_2d_3d_info(M, W, U, V, u_op, v_op, u_ad, v_ad, False)

    assert u_struct is u_ad
    assert v_struct is v_ad
    np.testing.assert_allclose(u_struct.n_3d, [0.0, 0.0, 1.0])
    np.testing.assert_allclose(v_struct.n_3d, [0.0, -1.0, 0.0])
    assert u_struct.cos == 1.0
    assert v_struct.cos == 1.0
