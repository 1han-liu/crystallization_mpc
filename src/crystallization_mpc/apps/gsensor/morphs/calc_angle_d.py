"""Translation of gsensor/morphs/calc_angle_d.m."""

import numpy as np


def calc_angle_d(line1, line2):
    line1 = np.asarray(line1, dtype=float)
    line2 = np.asarray(line2, dtype=float)
    angle_d = np.degrees(
        np.arccos(np.dot(line1, line2) / (np.linalg.norm(line1) * np.linalg.norm(line2)))
    )
    return float(angle_d)


__all__ = ["calc_angle_d"]
