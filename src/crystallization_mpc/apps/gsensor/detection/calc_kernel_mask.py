"""Translation of gsensor/detection/calc_kernel_mask.m."""

import numpy as np


def calc_kernel_mask(I, kernel):
    I = np.asarray(I)
    size_I = I.shape
    x, y = np.meshgrid(np.arange(size_I[1]), np.arange(size_I[0]))
    kernel_mask = np.ones(I.shape, dtype=bool)
    num_corners = len(kernel.k_c_cell)
    for ii in range(num_corners):
        p1 = np.asarray(kernel.k_c_cell[ii])
        p2 = np.asarray(kernel.k_c_cell[(ii + 1) % num_corners])
        tau = p2 - p1
        n = np.array([tau[1], -tau[0], 0])
        o = np.asarray(kernel.k_o_cell[ii])
        sign = (p1[0] - o[0]) * n[0] + (p1[1] - o[1]) * n[1]
        mask_tmp = sign * ((p1[0] - x) * n[0] + (p1[1] - y) * n[1]) < 0
        kernel_mask = kernel_mask * mask_tmp
    kernel_mask = kernel_mask > 0
    return kernel_mask
