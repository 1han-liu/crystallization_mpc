"""Translation of gsensor/utils/make_line.m."""


def make_line(ax, p1, p2, line_style):
    plot(ax, [p1[0], p2[0]], [p1[1], p2[1]], LineStyle=line_style)
