"""Translation of gsensor/morphs/calc_hough_info.m."""

import numpy as np


def calc_hough_info(uv_struct):
    theta = np.degrees(np.arccos(np.dot(uv_struct.n, [1, 0, 0]))) * np.sign(uv_struct.n[1])
    rho = np.dot(uv_struct.n, uv_struct.t)
    is_opposite = np.dot(uv_struct.o - uv_struct.t, uv_struct.n) < 0
    return theta, rho, is_opposite
