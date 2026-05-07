"""Translation of gsensor/utils/annotate_point.m."""


def annotate_point(ax, p, p_name):
    text(ax, p[0] + 20, p[1] - 20, p_name)
