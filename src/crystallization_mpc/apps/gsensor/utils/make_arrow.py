"""Translation of gsensor/utils/make_arrow.m."""


def make_arrow(ax, p1, p2):
    quiver(p1[0], p1[1], p2[0] - p1[0], p2[1] - p1[1], 0)
