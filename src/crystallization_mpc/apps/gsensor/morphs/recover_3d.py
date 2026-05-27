"""Translation of gsensor/morphs/recover_3d.m."""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize

from crystallization_mpc.apps.gsensor.morphs.calc_angle_d import calc_angle_d
from crystallization_mpc.apps.gsensor.morphs.get_angles_d import get_angles_d


def recover_3d(m, w, u, v, corner, is_full, direction):
    """Recover fixed 2D M/W/U/V coordinates by optimizing W/U/V z values."""

    UMW, VMW, UMV = get_angles_d(corner)
    M = np.array([*np.asarray(m, dtype=float).reshape(-1)[:2], 0.0], dtype=float)

    def W_z(z_w):
        return np.array([*np.asarray(w, dtype=float).reshape(-1)[:2], z_w], dtype=float)

    def U_z(z_u):
        return np.array([*np.asarray(u, dtype=float).reshape(-1)[:2], z_u], dtype=float)

    def V_z(z_v):
        return np.array([*np.asarray(v, dtype=float).reshape(-1)[:2], z_v], dtype=float)

    def UMN_z(z_w, z_u):
        return calc_angle_d(W_z(z_w) - M, U_z(z_u) - M)

    def VMN_z(z_w, z_v):
        return calc_angle_d(W_z(z_w) - M, V_z(z_v) - M)

    def UMV_z(z_u, z_v):
        return calc_angle_d(U_z(z_u) - M, V_z(z_v) - M)

    def f(z):
        return np.linalg.norm(
            np.array(
                [
                    UMN_z(z[0], z[1]) - UMW,
                    VMN_z(z[0], z[2]) - VMW,
                    UMV_z(z[1], z[2]) - UMV,
                ],
                dtype=float,
            )
        )

    z_0 = np.array([100.0, 100.0, 100.0], dtype=float)
    A = np.empty((0, 3), dtype=float)
    b = np.empty((0,), dtype=float)
    z_min = np.array([0.0, 0.0, 0.0], dtype=float)
    z_max = np.array([np.inf, np.inf, np.inf], dtype=float)

    if not is_full:
        z_0, A, b, z_min, z_max = _direction_constraints(direction)

    constraints = []
    if A.size:
        constraints.append({"type": "ineq", "fun": lambda z: b - A @ z})

    result = minimize(
        f,
        z_0,
        method="SLSQP",
        bounds=list(zip(z_min, z_max)),
        constraints=constraints,
        options={"disp": False},
    )
    if not result.success:
        raise RuntimeError(f"recover_3d optimization failed: {result.message}")

    z = result.x
    W = W_z(z[0])
    U = U_z(z[1])
    V = V_z(z[2])
    return M, W, U, V


def _direction_constraints(direction: str):
    z_0 = np.array([100.0, 100.0, 100.0], dtype=float)
    A = np.empty((0, 3), dtype=float)
    b = np.empty((0,), dtype=float)
    z_min = np.array([0.0, 0.0, 0.0], dtype=float)
    z_max = np.array([np.inf, np.inf, np.inf], dtype=float)

    match direction:
        case "inwards":
            z_0 = np.array([-100.0, 100.0, 100.0])
            A = np.array([[1.0, -1.0, 0.0], [1.0, 0.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, 0.0, 0.0])
            z_max = np.array([0.0, np.inf, np.inf])
        case "extra_inwards":
            z_0 = np.array([-100.0, -100.0, -100.0])
            A = np.array([[1.0, -1.0, 0.0], [1.0, 0.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, -np.inf, -np.inf])
            z_max = np.array([0.0, 0.0, 0.0])
        case "outwards":
            z_0 = np.array([100.0, 100.0, 100.0])
            A = np.array([[1.0, -1.0, 0.0], [1.0, 0.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, 0.0, 0.0])
            z_max = np.array([np.inf, np.inf, np.inf])
        case "in-1-1":
            z_0 = np.array([100.0, 100.0, 100.0])
            A = np.array([[-1.0, 1.0, 0.0], [0.0, -1.0, 1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, 0.0, 0.0])
            z_max = np.array([np.inf, np.inf, np.inf])
        case "in-1-2":
            z_0 = np.array([100.0, 100.0, 100.0])
            A = np.array([[-1.0, 0.0, 1.0], [0.0, 1.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, 0.0, 0.0])
            z_max = np.array([np.inf, np.inf, np.inf])
        case "in-2-1":
            z_0 = np.array([100.0, 100.0, -100.0])
            A = np.array([[-1.0, 1.0, 0.0], [0.0, -1.0, 1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, 0.0, -np.inf])
            z_max = np.array([np.inf, np.inf, 0.0])
        case "in-2-2":
            z_0 = np.array([100.0, -100.0, 100.0])
            A = np.array([[-1.0, 0.0, 1.0], [0.0, 1.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, -np.inf, 0.0])
            z_max = np.array([np.inf, 0.0, np.inf])
        case "in-3-1":
            z_0 = np.array([100.0, -100.0, -100.0])
            A = np.array([[-1.0, 1.0, 0.0], [0.0, -1.0, 1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, -np.inf, -np.inf])
            z_max = np.array([np.inf, 0.0, 0.0])
        case "in-3-2":
            z_0 = np.array([100.0, -100.0, -100.0])
            A = np.array([[-1.0, 0.0, 1.0], [0.0, 1.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, -np.inf, -np.inf])
            z_max = np.array([np.inf, 0.0, 0.0])
        case "in-4-1":
            z_0 = np.array([-100.0, -100.0, -100.0])
            A = np.array([[-1.0, 1.0, 0.0], [0.0, -1.0, 1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, -np.inf, -np.inf])
            z_max = np.array([0.0, 0.0, 0.0])
        case "in-4-2":
            z_0 = np.array([-100.0, -100.0, -100.0])
            A = np.array([[-1.0, 0.0, 1.0], [0.0, 1.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, -np.inf, -np.inf])
            z_max = np.array([0.0, 0.0, 0.0])
        case "out-1-1":
            z_0 = np.array([-100.0, -100.0, -100.0])
            A = np.array([[1.0, -1.0, 0.0], [0.0, 1.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, -np.inf, -np.inf])
            z_max = np.array([0.0, 0.0, 0.0])
        case "out-1-2":
            z_0 = np.array([-100.0, -100.0, -100.0])
            A = np.array([[1.0, 0.0, -1.0], [0.0, -1.0, 1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, -np.inf, -np.inf])
            z_max = np.array([0.0, 0.0, 0.0])
        case "out-2-1":
            z_0 = np.array([-100.0, -100.0, 100.0])
            A = np.array([[1.0, -1.0, 0.0], [0.0, 1.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, -np.inf, 0.0])
            z_max = np.array([0.0, 0.0, np.inf])
        case "out-2-2":
            z_0 = np.array([-100.0, 100.0, -100.0])
            A = np.array([[1.0, 0.0, -1.0], [0.0, -1.0, 1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, 0.0, -np.inf])
            z_max = np.array([0.0, np.inf, 0.0])
        case "out-3-1":
            z_0 = np.array([-100.0, 100.0, 100.0])
            A = np.array([[1.0, -1.0, 0.0], [0.0, 1.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, 0.0, 0.0])
            z_max = np.array([0.0, np.inf, np.inf])
        case "out-3-2":
            z_0 = np.array([-100.0, 100.0, 100.0])
            A = np.array([[1.0, 0.0, -1.0], [0.0, -1.0, 1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([-np.inf, 0.0, 0.0])
            z_max = np.array([0.0, np.inf, np.inf])
        case "out-4-1":
            z_0 = np.array([100.0, 100.0, 100.0])
            A = np.array([[1.0, -1.0, 0.0], [0.0, 1.0, -1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, 0.0, 0.0])
            z_max = np.array([np.inf, np.inf, np.inf])
        case "out-4-2":
            z_0 = np.array([100.0, 100.0, 100.0])
            A = np.array([[1.0, 0.0, -1.0], [0.0, -1.0, 1.0]])
            b = np.array([0.0, 0.0])
            z_min = np.array([0.0, 0.0, 0.0])
            z_max = np.array([np.inf, np.inf, np.inf])
        case _:
            pass

    return z_0, A, b, z_min, z_max


__all__ = ["recover_3d"]
