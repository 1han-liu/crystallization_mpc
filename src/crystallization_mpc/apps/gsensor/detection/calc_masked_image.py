"""Translation of gsensor/detection/calc_masked_image.m."""

import numpy as np


def calc_masked_image(I, t, e, n, width, ratio):
    I = np.asarray(I)
    t = np.asarray(t)
    e = np.asarray(e)
    n = np.asarray(n)

    up = t + n * width
    dp = t - n * width
    s = e - t
    rp = e + (e - t) * ratio
    lp = t - (e - t) * ratio
    size_I = I.shape
    x, y = np.meshgrid(np.arange(size_I[1]), np.arange(size_I[0]))
    mask1 = ((up[0] - x) * n[0] + (up[1] - y) * n[1]) * (
        (dp[0] - x) * n[0] + (dp[1] - y) * n[1]
    ) < 0
    # mask2 = ((rp[0] - x) * s[0] + (rp[1] - y) * s[1]) * (
    #     (lp[0] - x) * s[0] + (lp[1] - y) * s[1]
    # ) < 0
    # mask = mask1 * mask2
    mask = mask1
    try:
        I = I * mask
    except Exception:
        I[~np.repeat(mask[:, :, np.newaxis], I.shape[2], axis=2)] = 0
    return I, mask
