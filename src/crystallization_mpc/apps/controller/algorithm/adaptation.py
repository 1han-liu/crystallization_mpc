"""Bound-constrained growth-parameter adaptation."""

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

    def __init__(self, message: str, *, reference_identifier: str | None = None):
        super().__init__(message)
        self.reference_identifier = reference_identifier


def _bounded_minimum(function, lower: float, upper: float, *, initial: float) -> float:
    # R2021a fmincon evaluates the objective and forward-difference derivative
    # at the supplied initial guess. In particular, negative sigma with an
    # integer n can have a real objective but an undefined derivative in n.
    def defined(value):
        return bool(np.isfinite(value) and np.imag(value) == 0)

    value = function(initial)
    if not defined(value):
        raise AdaptationError(
            "Objective function is undefined at initial point. Fmincon cannot continue.",
            reference_identifier="optim:barrier:UsrObjUndefAtX0",
        )
    delta = np.sqrt(np.finfo(float).eps) * max(abs(initial), 1.0)
    shifted = initial + delta if initial + delta <= upper else initial - delta
    if not defined(function(shifted)):
        raise AdaptationError(
            "Finite difference derivatives at initial point contain Inf, NaN, or complex values. Fmincon cannot continue.",
            reference_identifier="optim:barrier:DerivUndefAtX0",
        )

    def real_objective(x):
        result = function(x)
        # A non-finite trial is not an input sample filter. The solver may
        # move away from it, as opposed to rejecting the initial objective.
        return float(np.real(result)) if defined(result) else math.inf

    result = minimize_scalar(
        real_objective,
        bounds=(float(lower), float(upper)),
        method="bounded",
        options={"maxiter": 100, "xatol": 1e-8},
    )
    if not math.isfinite(float(result.x)) or not math.isfinite(float(result.fun)):
        raise AdaptationError(f"Growth adaptation failed: {result.message}")
    # The MATLAB caller requests b_min only, not exitflag. Do not add an
    # independent success-flag gate for a finite returned iterate.
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
    R = float(updated["R"])

    def residual(n: float, log_k_0: float, E_A: float):
        # Preserve log(k0 * sigma.^n * exp(...)), including MATLAB's complex
        # power/log semantics. n*log(sigma) is not equivalent for negative
        # sigma and changes both domain failures and underflow behavior.
        with np.errstate(all="ignore"):
            prediction = np.exp(log_k_0) * np.emath.power(sigma, n) * np.exp(-E_A / R / temperature)
            return np.sum((np.emath.log(prediction) - np.log(growth)) ** 2)

    if adaptive_mode in {"E_A", "E_A_and_k_0", "E_A_and_n", "all"}:
        current = float(updated["E_A"])
        updated["E_A"] = abs(
            _bounded_minimum(
                lambda value: residual(
                    float(updated["n"]), math.log(float(updated["k_0"])), value
                ),
                current / 2.0,
                current * 2.0,
                initial=current,
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
            initial=current,
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
                initial=current,
            )
        )

    return updated, count


__all__ = [
    "ADAPTATION_MODES",
    "AdaptationError",
    "adapt_growth_parameters",
]
