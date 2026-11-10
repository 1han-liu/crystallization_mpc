"""Crystallization process dynamics and RK45 state transitions."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ._ode45_ieee import ode45_final_ieee
from .._recovery import validate_measurement, validate_model_state
from .thermodynamics import calc_c_sat, calc_dc_sat_dT, calc_relative_sigma


ODE_RTOL = 1e-3
ODE_ATOL = 1e-6


def calc_mode() -> str:
    """Reproduce frozen `calc_mode.m`, including its hard-coded MPC behavior."""

    return "MPC"


def state_transition_matrices_model(
    n: float, E_A: float, R: float, x: np.ndarray, dt: float
) -> tuple[np.ndarray, np.ndarray]:
    del dt  # The reference signature retains dt but the matrix does not use it.
    T, _dT_dt, c, dc_dt = np.asarray(x, dtype=float).reshape(4)
    c_sat = float(calc_c_sat(T))
    dc_sat_dT = float(calc_dc_sat_dT(T))
    if abs(float(calc_relative_sigma(c, T))) < 0.001:
        A31 = 0.0
        A33 = 0.0
    else:
        A31 = dc_dt * (
            -n * c * dc_sat_dT / c_sat / (c - c_sat) + E_A / R / (T * T)
        )
        A33 = dc_dt * n / (c - c_sat)
    A = np.array(
        [
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0],
            [A31, 0.0, A33, 0.0],
            [0.0, A31, 0.0, A33],
        ],
        dtype=float,
    )
    Bu = np.array([0.0, 0.0, dc_dt - A33 * c - A31 * T, 0.0], dtype=float)
    return A, Bu


def state_transition_matrices(
    params: Mapping[str, Any], x: np.ndarray, dt: float
) -> tuple[np.ndarray, np.ndarray]:
    # D-001: this helper follows calc_mode(), not the explicit UI mode.
    if calc_mode() == "MPC":
        return state_transition_matrices_model(
            float(params["n"]),
            float(params["E_A"]),
            float(params["R"]),
            np.asarray(x, dtype=float),
            float(dt),
        )
    raise AssertionError("Frozen calc_mode() must return MPC.")


def state_transition_ode(
    params: Mapping[str, Any], x: np.ndarray, dt: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    A, Bu = state_transition_matrices(params, x, dt)
    x_array = np.asarray(x, dtype=float).reshape(4)
    return A @ x_array + Bu, A, Bu


def state_transition_function(
    params: Mapping[str, Any],
    x: np.ndarray,
    dt: float,
    *,
    rtol: float = ODE_RTOL,
    atol: float = ODE_ATOL,
) -> np.ndarray:
    validate_model_state(x, stage="transition_initial_state")
    initial = np.asarray(x, dtype=float).reshape(4)

    def rhs(_time: float, state: np.ndarray) -> np.ndarray:
        return state_transition_ode(params, state, dt)[0]

    result = ode45_final_ieee(rhs, initial, dt, rtol=rtol, atol=atol)
    validate_model_state(result, stage="transition_final_state")
    return result


def state_transition_ode_T(params: Mapping[str, Any], T: float, T_j: float) -> float:
    tau_1 = float(params["tau_1"])
    tau_2 = float(params["tau_2"])
    T_R = float(params["T_R"])
    return -(1.0 / tau_1 + 1.0 / tau_2) * T + 1.0 / tau_1 * T_j + 1.0 / tau_2 * T_R


def state_transition_function_T(
    params: Mapping[str, Any],
    T: float,
    T_j: float,
    dt: float,
    *,
    rtol: float = ODE_RTOL,
    atol: float = ODE_ATOL,
) -> float:
    validate_measurement("T", T, stage="thermal_transition_initial")
    validate_measurement("T_j", T_j, stage="thermal_transition_initial")
    result = ode45_final_ieee(
        lambda _time, state: np.array(
            [state_transition_ode_T(params, float(state[0]), float(T_j))]
        ),
        np.array([float(T)]),
        float(dt),
        rtol=rtol,
        atol=atol,
    )
    value = float(result[0])
    validate_measurement("T", value, stage="thermal_transition_final")
    return value


__all__ = [
    "ODE_ATOL",
    "ODE_RTOL",
    "calc_mode",
    "state_transition_function",
    "state_transition_function_T",
    "state_transition_matrices",
    "state_transition_matrices_model",
    "state_transition_ode",
    "state_transition_ode_T",
]
