"""Translation of gsensor/morphs/calc_hough_info.m."""

import numpy as np


def calc_hough_info(uv_struct):
    normal = np.asarray(uv_struct.n, dtype=float).reshape(-1)
    theta = float(np.degrees(np.arctan2(normal[1], normal[0])))
    rho = float(np.dot(normal, np.asarray(uv_struct.t, dtype=float).reshape(-1)))
    theta, rho = _normalize_hough_line(theta, rho)
    is_opposite = np.dot(uv_struct.o - uv_struct.t, uv_struct.n) < 0
    return theta, rho, is_opposite


def _normalize_hough_line(theta, rho):
    """Use the same unique theta/rho branch as MATLAB/OpenCV Hough output.

    A geometric line has two equivalent normal representations.  Keeping theta
    in ``[-90, 90)`` and flipping rho at the same time prevents one edge from
    being rejected merely because its initialization normal points the other
    way.
    """

    theta = float(theta)
    rho = float(rho)
    while theta >= 90.0:
        theta -= 180.0
        rho = -rho
    while theta < -90.0:
        theta += 180.0
        rho = -rho
    return theta, rho
