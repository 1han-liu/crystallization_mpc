"""MATLAB-compatible bound-only growth-parameter adaptation."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize_scalar


ADAPTATION_MODES = (
    "E_A",
    "k_0",
    "n",
    "E_A_and_k_0",
    "E_A_and_n",
    "k_0_and_n",
    "all",
)


class AdaptationError(RuntimeError):
    """Expected numerical failure during parameter adaptation."""


def _bounded_minimum(function, lower: float, upper: float) -> float:
    result = minimize_scalar(
        function,
        bounds=(float(lower), float(upper)),
        method="bounded",
        options={"maxiter": 100, "xatol": 1e-8},
    )
    if not result.success or not math.isfinite(float(result.x)):
        raise AdaptationError(f"Growth adaptation failed: {result.message}")
    return float(result.x)


def adapt_growth_parameters(
    params: Mapping[str, Any],
    G_measure_KF_list: Sequence[float],
    sigma_list: Sequence[float],
    T_list: Sequence[float],
    to_adapt_list: Sequence[bool],
    max_num_adapt: int,
    min_num_adapt: int,
    adaptive_mode: str,
) -> tuple[dict[str, Any], int]:
    if adaptive_mode not in ADAPTATION_MODES:
        raise ValueError("Unsupported adaptation mode.")
    growth = np.asarray(G_measure_KF_list, dtype=float)
    sigma = np.asarray(sigma_list, dtype=float)
    temperature = np.asarray(T_list, dtype=float)
    selected = np.asarray(to_adapt_list, dtype=bool)
    if not (growth.shape == sigma.shape == temperature.shape == selected.shape):
        raise ValueError("Adaptation histories must have equal shapes.")
    growth = growth[selected]
    sigma = sigma[selected]
    temperature = temperature[selected]
    keep = ~np.isnan(growth)
    growth, sigma, temperature = growth[keep], sigma[keep], temperature[keep]
    keep = growth > 0
    growth, sigma, temperature = growth[keep], sigma[keep], temperature[keep]
    if growth.size > int(max_num_adapt):
        growth = growth[-int(max_num_adapt) :]
        sigma = sigma[-int(max_num_adapt) :]
        temperature = temperature[-int(max_num_adapt) :]
    sigma[np.abs(sigma) < 1e-15] = 1e-15
    count = int(growth.size)
    updated = dict(params)
    if count < int(min_num_adapt):
        return updated, count
    if not np.isfinite(sigma).all() or not np.isfinite(temperature).all():
        raise AdaptationError("Adaptation inputs contain non-finite sigma or temperature.")
    if np.any(sigma <= 0):
        raise AdaptationError(
            "Adaptation requires positive supersaturation for the logarithmic model."
        )
    if np.any(temperature <= 0):
        raise AdaptationError("Adaptation temperatures must be positive kelvin.")

    log_growth = np.log(growth)
    R = float(updated["R"])

    def residual(n: float, log_k_0: float, E_A: float) -> float:
        predicted_log = log_k_0 + n * np.log(sigma) - E_A / R / temperature
        value = float(np.sum((predicted_log - log_growth) ** 2))
        return value if math.isfinite(value) else math.inf

    if adaptive_mode in {"E_A", "E_A_and_k_0", "E_A_and_n", "all"}:
        current = float(updated["E_A"])
        updated["E_A"] = abs(
            _bounded_minimum(
                lambda value: residual(
                    float(updated["n"]), math.log(float(updated["k_0"])), value
                ),
                current / 2.0,
                current * 2.0,
            )
        )

    if adaptive_mode in {"k_0", "E_A_and_k_0", "k_0_and_n", "all"}:
        current = math.log(float(updated["k_0"]))
        optimum = _bounded_minimum(
            lambda value: residual(
                float(updated["n"]), value, float(updated["E_A"])
            ),
            current / 2.0,
            current * 2.0,
        )
        updated["k_0"] = math.exp(optimum)

    if adaptive_mode in {"n", "E_A_and_n", "k_0_and_n", "all"}:
        current = float(updated["n"])
        updated["n"] = abs(
            _bounded_minimum(
                lambda value: residual(
                    value, math.log(float(updated["k_0"])), float(updated["E_A"])
                ),
                current / 2.0,
                current * 2.0,
            )
        )

    return updated, count


__all__ = [
    "ADAPTATION_MODES",
    "AdaptationError",
    "adapt_growth_parameters",
]
