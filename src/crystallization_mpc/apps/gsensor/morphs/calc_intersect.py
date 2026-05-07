"""Translation of gsensor/morphs/calc_intersect.m."""

import numpy as np


def calc_intersect(line1_1, line1_2, line2_1, line2_2):
    A = np.asarray(line1_1[:2]).reshape(2, 1)
    B = np.asarray(line1_2[:2]).reshape(2, 1)
    C = np.asarray(line2_1[:2]).reshape(2, 1)
    D = np.asarray(line2_2[:2]).reshape(2, 1)
    lambda_ = np.linalg.solve(np.hstack((B - A, C - D)), C - A)
    E = A + lambda_[0] * (B - A)
    s = np.array([E[0, 0], E[1, 0], 0])
    return s
