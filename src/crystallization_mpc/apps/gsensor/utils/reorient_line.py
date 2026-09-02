"""Translation of gsensor/utils/reorient_line.m."""

from .reorient_points import reorient_points


def reorient_line(line):
    line.point1, line.point2 = reorient_points(line.point1, line.point2)
    return line
