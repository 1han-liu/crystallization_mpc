"""Translation of gsensor/detection/calc_theta_range.m."""

import numpy as np


def calc_theta_range(theta, delta_theta):
    theta_range = np.linspace(theta - delta_theta, theta + delta_theta, 20)
    theta_range = np.sort(np.mod(theta_range + 90, 180) - 90)
    return theta_range
