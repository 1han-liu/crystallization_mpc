"""Translation of gsensor/detection/initialze_line.m."""


def initialize_line(t, e, theta_0, rho_0):
    line = type("line", (), {})()
    line.point1 = t
    line.point2 = e
    line.theta = theta_0
    line.rho = rho_0
    return line
