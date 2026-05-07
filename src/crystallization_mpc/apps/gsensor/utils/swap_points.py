"""Translation of gsensor/utils/swap_points.m."""


def swap_points(p1, p2):
    tmp = p1
    p1 = p2
    p2 = tmp
    return p1, p2
