"""Controller target, lag, PI/MPC and jacket calculations."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize_scalar

from .dynamics import state_transition_matrices
from .thermodynamics import calc_G, calc_relative_sigma


class OptimizationError(RuntimeError):
    """Expected numerical optimizer failure."""


def clip(value: float, lower: float, upper: float) -> float:
    return max(float(lower), min(float(upper), float(value)))


def add_to_int_X_dt(error: float, integral: float, dt: float) -> float:
    return float(integral) + float(error) * float(dt)


def calc_dT_dt(values: Sequence[float], dt: float) -> float:
    return 0.0 if len(values) < 2 else (float(values[-1]) - float(values[-2])) / dt


def calc_dc_dt(values: Sequence[float], dt: float) -> float:
    return calc_dT_dt(values, dt)


def calc_dT_dt_model(params: Mapping[str, Any], x: np.ndarray, T_j: float) -> float:
    T = float(np.asarray(x, dtype=float).reshape(4)[0])
    tau_1 = float(params["tau_1"])
    tau_2 = float(params["tau_2"])
    return (
        float(T_j) / tau_1
        - (1.0 / tau_1 + 1.0 / tau_2) * T
        + float(params["T_R"]) / tau_2
    )


def calc_T_j(params: Mapping[str, Any], x: np.ndarray, dT_dt_set: float) -> float:
    T = float(np.asarray(x, dtype=float).reshape(4)[0])
    tau_1 = float(params["tau_1"])
    tau_2 = float(params["tau_2"])
    return (
        tau_1 * float(dT_dt_set)
        + (1.0 + tau_1 / tau_2) * T
        - tau_1 / tau_2 * float(params["T_R"])
    )


def calc_T_with_zero_dT_dt(params: Mapping[str, Any], T_j: float) -> float:
    tau_1 = float(params["tau_1"])
    tau_2 = float(params["tau_2"])
    return (float(T_j) / tau_1 + float(params["T_R"]) / tau_2) / (
        1.0 / tau_1 + 1.0 / tau_2
    )


def calc_e_dT_dt(
    params: Mapping[str, Any], x: np.ndarray, T_j: float, dT_dt_set: float
) -> float:
    return calc_dT_dt_model(params, x, T_j) - float(dT_dt_set)


def _signed_root(value: float, order: float) -> float:
    return math.copysign(abs(float(value)) ** (1.0 / float(order)), float(value))


def calc_e_target(
    params: Mapping[str, Any], target: str, target_set: float, c: float, T: float
) -> float:
    sigma = float(calc_relative_sigma(c, T))
    if target == "sigma":
        target_value = sigma
    elif target == "G":
        growth = float(calc_G(params, c, T, is_model=False))
        k_T = float(params["k_0"]) * math.exp(
            -float(params["E_A"]) / float(params["R"]) / float(T)
        )
        target_value = _signed_root(growth / k_T, float(params["n"]))
        target_set = _signed_root(float(target_set) / k_T, float(params["n"]))
    else:
        raise ValueError("target must be sigma or G.")
    return target_value - float(target_set)


def objective_function(
    params: Mapping[str, Any],
    x: np.ndarray,
    target: str,
    dT_dt_set: float,
    target_set: float,
    dt: float,
) -> float:
    state = np.asarray(x, dtype=float).reshape(4)
    A, Bu = state_transition_matrices(params, state, dt)
    T, dT_dt, c, _dc_dt = state
    dc_dt_model = float(A[2, :] @ state + Bu[2])
    d2c_dt2 = float(A[3, :] @ state + Bu[3])
    T_next = T + float(dT_dt_set) * float(dt)
    c_next = c + dc_dt_model * float(dt) + d2c_dt2 * float(dt) ** 2 / 2.0
    error = calc_e_target(params, target, target_set, c_next, T_next)
    weight = float(params["w_sigma"] if target == "sigma" else params["w_G"])
    return float(
        weight * error**2
        + float(params["w_dT_dt"]) * (float(dT_dt_set) - dT_dt) ** 2
    )


def calc_dT_dt_set(
    params: Mapping[str, Any],
    x: np.ndarray,
    mode: str,
    target: str,
    target_set: float,
    dT_dt_min: float,
    dT_dt_max: float,
    dt: float,
    int_e_target_dt: float,
) -> tuple[float, float]:
    state = np.asarray(x, dtype=float).reshape(4)
    error = calc_e_target(params, target, target_set, state[2], state[0])
    integral = add_to_int_X_dt(error, int_e_target_dt, dt)
    if mode == "MPC":
        result = minimize_scalar(
            lambda value: objective_function(
                params, state, target, float(value), target_set, dt
            ),
            bounds=(float(dT_dt_min), float(dT_dt_max)),
            method="bounded",
            options={"xatol": 1e-4},
        )
        if not result.success or not math.isfinite(float(result.x)):
            raise OptimizationError(f"MPC optimization failed: {result.message}")
        value = float(result.x)
    elif mode == "PI":
        value = clip(
            float(params["K_P_target"]) * error
            + float(params["K_I_target"]) * integral,
            dT_dt_min,
            dT_dt_max,
        )
    else:
        raise ValueError("mode must be MPC or PI.")
    return value, integral


def calc_T_j_set_inner(
    params: Mapping[str, Any],
    x: np.ndarray,
    dT_dt_set: float,
    int_e_dT_dt_dt: float,
    int_e_T_dt: float,
    T_j: float,
    T_j_min: float,
    T_j_max: float,
    dt: float,
) -> tuple[float, float, float]:
    T_j_calc = calc_T_j(params, x, dT_dt_set)
    e_dT_dt = calc_e_dT_dt(params, x, T_j_calc, dT_dt_set)
    next_int_dT = add_to_int_X_dt(e_dT_dt, int_e_dT_dt_dt, dt)
    T = float(np.asarray(x, dtype=float).reshape(4)[0])
    e_T = max(0.0, T - calc_T_with_zero_dT_dt(params, T_j))
    next_int_T = add_to_int_X_dt(e_T, int_e_T_dt, dt)
    # D-002: frozen calc_T_j_set_.m reads K_P_target into local K_P_T.
    T_j_set = (
        T_j_calc
        - float(params["K_P_dT_dt"]) * e_dT_dt
        - float(params["K_I_dT_dt"]) * next_int_dT
        - float(params["K_P_target"]) * e_T
        - float(params["K_I_T"]) * next_int_T
    )
    return clip(T_j_set, T_j_min, T_j_max), next_int_dT, next_int_T


def calc_T_j_set(
    params: Mapping[str, Any],
    x: np.ndarray,
    dT_dt_set: float,
    int_e_dT_dt_dt: float,
    int_e_T_dt: float,
    T_j: float,
    T_j_min: float,
    T_j_max: float,
    dt: float,
    non_increasing: bool,
) -> tuple[float, float, float]:
    selected = calc_T_j_set_inner(
        params,
        x,
        dT_dt_set,
        int_e_dT_dt_dt,
        int_e_T_dt,
        T_j,
        T_j_min,
        T_j_max,
        dt,
    )
    if non_increasing:
        zero = calc_T_j_set_inner(
            params,
            x,
            0.0,
            int_e_dT_dt_dt,
            int_e_T_dt,
            T_j,
            T_j_min,
            T_j_max,
            dt,
        )
        if selected[0] > zero[0]:
            selected = zero
    return selected


def calc_t_lag_perc(
    base_percentage: float,
    threshold_percentage: float,
    target_set: float,
    absolute_error: float,
) -> float:
    ratio = math.inf if absolute_error == 0 else (
        float(threshold_percentage) * float(target_set) / float(absolute_error)
    )
    return 1.0 + float(base_percentage) * math.atan(ratio) / (math.pi / 2.0)


def calc_t_lag_perc2(
    base_percentage: float, dT_dt_ratio: float, dc_dt_ratio: float
) -> float:
    del dT_dt_ratio
    return 1.0 - float(base_percentage) * math.atan(
        max(float(dc_dt_ratio), 1.0) - 1.0
    ) / (math.pi / 2.0)


def calc_dT_j_dt(
    T_j: float, dT_j_dt_max: float, T_j_min: float, T_j_max: float
) -> float:
    lower = 273.15 + 5.0
    upper = 273.15 + 70.0
    if lower <= T_j <= upper:
        return float(dT_j_dt_max)
    if T_j > upper:
        return (float(T_j_max) - T_j) / (float(T_j_max) - upper) * float(
            dT_j_dt_max
        )
    if T_j < lower:
        return (T_j - float(T_j_min)) / (lower - float(T_j_min)) * float(
            dT_j_dt_max
        )
    return 0.0


def update_T_j(
    T_j_set: float,
    T_j: float,
    dT_j_dt_max: float,
    dt: float,
    dT_j: float,
    dt_update: float,
    T_j_min: float,
    T_j_max: float,
) -> float:
    frequency = math.trunc(float(dt) / float(dt_update))
    value = float(T_j)
    if frequency <= 0:
        return value
    for _index in range(frequency):
        delta = float(T_j_set) - value
        if abs(delta) > float(dT_j):
            value += (
                calc_dT_j_dt(value, dT_j_dt_max, T_j_min, T_j_max)
                * math.copysign(1.0, delta)
                * float(dt)
                / frequency
            )
    return value


__all__ = [
    "OptimizationError",
    "add_to_int_X_dt",
    "calc_T_j",
    "calc_T_j_set",
    "calc_T_j_set_inner",
    "calc_T_with_zero_dT_dt",
    "calc_dT_dt",
    "calc_dT_dt_model",
    "calc_dT_dt_set",
    "calc_dT_j_dt",
    "calc_dc_dt",
    "calc_e_dT_dt",
    "calc_e_target",
    "calc_t_lag_perc",
    "calc_t_lag_perc2",
    "clip",
    "objective_function",
    "update_T_j",
]
