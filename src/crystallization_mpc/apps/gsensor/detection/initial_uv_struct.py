"""Translation of gsensor/detection/initial_uv_struct.m."""

from types import SimpleNamespace

from .create_EKF import create_EKF_G


def initialize_uv_struct(uv_struct, dt_G, resolution, q2, r_diag):
    line = SimpleNamespace(
        point1=uv_struct.t,
        point2=uv_struct.e,
        theta=uv_struct.theta_0,
        rho=uv_struct.rho_0,
    )
    uv_struct.line = line
    x_G = [[0], [0], [0]]
    uv_struct.EKF_G = create_EKF_G(dt_G, resolution, q2, r_diag, x_G)
    uv_struct.x_G_array = []
    uv_struct.dist_array = []
    uv_struct.dist_KF_array = []
    uv_struct.G_array = []
    uv_struct.G_KF_array = []

    return uv_struct
