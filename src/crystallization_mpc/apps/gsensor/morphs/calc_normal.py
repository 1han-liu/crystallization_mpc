"""Translation of gsensor/morphs/calc_normal.m."""

import numpy as np


def calc_normal(t, e):
    t = np.asarray(t)
    e = np.asarray(e)
    n = np.cross(e - t, [0, 0, 1])
    n = n / np.linalg.norm(n)
    vc = (t + e) / 2
    v = vc + np.linalg.norm(e - t) * n / 10
    return n, v, vc
