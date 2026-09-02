"""Translation of gsensor/morphs/calc_foot_point.m."""

import numpy as np


def calc_foot_point(m, foot_ad_struct):
    m = np.asarray(m)
    t = np.asarray(foot_ad_struct.t)
    e = np.asarray(foot_ad_struct.e)
    foot = (t + e) / 2 + 1 * np.linalg.norm(e - t) * (t - m) / np.linalg.norm(t - m)
    return foot
