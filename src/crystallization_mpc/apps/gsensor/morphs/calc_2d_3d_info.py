"""Translation of gsensor/morphs/calc_2d_3d_info.m."""

import numpy as np

from crystallization_mpc.apps.gsensor.morphs.calc_hough_info import calc_hough_info


def calc_2d_3d_info(
    M,
    W,
    U,
    V,
    u_op_struct,
    v_op_struct,
    u_ad_struct,
    v_ad_struct,
    is_full,
):
    M = np.asarray(M, dtype=float)
    W = np.asarray(W, dtype=float)
    UV_list = [np.asarray(U, dtype=float), np.asarray(V, dtype=float)]
    if is_full:
        uv_struct_list = [u_op_struct, v_op_struct]
    else:
        uv_struct_list = [u_ad_struct, v_ad_struct]

    for kk in range(2):
        n_3d = np.cross(W - M, UV_list[kk] - M)
        uv_struct_list[kk].n_3d = n_3d / np.linalg.norm(n_3d)
        uv_struct_list[kk].cos = abs(np.dot(uv_struct_list[kk].n_3d, uv_struct_list[kk].n)) / (
            np.linalg.norm(uv_struct_list[kk].n_3d) * np.linalg.norm(uv_struct_list[kk].n)
        )
        (
            uv_struct_list[kk].theta_0,
            uv_struct_list[kk].rho_0,
            uv_struct_list[kk].is_opposite,
        ) = calc_hough_info(uv_struct_list[kk])

    u_struct = uv_struct_list[0]
    v_struct = uv_struct_list[1]
    return u_struct, v_struct


__all__ = ["calc_2d_3d_info"]
