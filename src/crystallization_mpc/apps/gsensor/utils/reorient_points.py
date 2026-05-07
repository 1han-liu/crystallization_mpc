"""Translation of gsensor/utils/reorient_points.m."""

from .swap_points import swap_points


def reorient_points(t, p):
    if t[1] > p[1]:
        t, p = swap_points(t, p)
    elif t[1] == p[1]:
        if t[0] > p[0]:
            t, p = swap_points(t, p)
    return t, p
