"""Bound-constrained growth-parameter adaptation."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
from ._bounded_scalar import bounded_scalar
from ._fdlibm import real_power
from ._optimizer_bounds import prepare_fmincon_start, ScalarOptimizerError


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


def _source_log(value: float):
    # Keep the scalar libm path for positive values. Extend its domain with
    # the source logarithm's -Inf/complex results instead of Python errors.
    if value == 0:
        return -math.inf
    if value < 0:
        return complex(math.log(-value), math.pi)
    return math.log(value)


def _source_exp(value: float) -> float:
    try:
        return math.exp(value)
    except OverflowError:
        return math.inf


def _bounded_minimum(function, lower: float, upper: float, *, initial: float) -> float:
    # R2021a fmincon evaluates the objective and forward-difference derivative
    # at the supplied initial guess. In particular, negative sigma with an
    # integer n can have a real objective but an undefined derivative in n.
    try:
        start = prepare_fmincon_start(lower, upper, initial)
    except ScalarOptimizerError as exc:
        raise AdaptationError(str(exc), reference_identifier=exc.reference_identifier) from exc
    lower, upper, initial = start.lower, start.upper, start.initial
    if start.infeasible:
        return initial

    def defined(value):
        return bool(np.isfinite(value) and np.imag(value) == 0)

    value = function(initial)
    if np.size(value) != 1:
        raise AdaptationError("Supplied objective function must return a scalar value.",
                              reference_identifier="optimlib:fmincon:NonScalarObj")
    if not defined(value):
        raise AdaptationError(
            "Objective function is undefined at initial point. Fmincon cannot continue.",
            reference_identifier="optim:barrier:UsrObjUndefAtX0",
        )
    delta = np.sqrt(np.finfo(float).eps) * max(abs(initial), 1.0)
    if initial < 0:
        delta = -delta
    shifted = (initial - delta if not lower <= initial + delta <= upper
               and lower <= initial - delta <= upper else initial + delta)
    shifted_value = function(shifted)
    if not defined(shifted_value):
        raise AdaptationError(
            "Finite difference derivatives at initial point contain Inf, NaN, or complex values. Fmincon cannot continue.",
            reference_identifier="optim:barrier:DerivUndefAtX0",
        )

    if lower == upper:
        return initial

    initial_evaluations = {float(initial): value, float(shifted): shifted_value}

    def real_objective(x):
        # These are the objective/forward-difference evaluations already made
        # by the solver's initial-domain check, not additional evaluations.
        result = initial_evaluations.pop(float(x)) if float(x) in initial_evaluations else function(x)
        # A non-finite trial is not an input sample filter. The solver may
        # move away from it, as opposed to rejecting the initial objective.
        return float(np.real(result)) if defined(result) else math.inf

    try:
        result = bounded_scalar(real_objective, float(lower), float(upper), initial=float(initial))
    except ValueError as exc:
        raise AdaptationError(f"Growth adaptation failed: {exc}") from exc
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
    growth = np.asarray(G_measure_KF_list, dtype=float)
    sigma = np.asarray(sigma_list, dtype=float)
    temperature = np.asarray(T_list, dtype=float)
    selected = np.asarray(to_adapt_list, dtype=bool)
    indices = np.flatnonzero(selected)
    # MATLAB logical indexing may have a shorter mask (or trailing false
    # entries). Only a true index outside a history is an indexing error.
    growth = growth[indices]
    sigma = sigma[indices]
    temperature = temperature[indices]
    keep = ~np.isnan(growth)
    growth, sigma, temperature = growth[keep], sigma[keep], temperature[keep]
    keep = growth > 0
    growth, sigma, temperature = growth[keep], sigma[keep], temperature[keep]
    first = float(np.fmax(1.0, growth.size - float(max_num_adapt) + 1.0))
    if first > growth.size:
        growth, sigma, temperature = growth[:0], sigma[:0], temperature[:0]
    elif first != math.floor(first):
        raise AdaptationError("Array indices must be positive integers or logical values.",
                              reference_identifier="MATLAB:badsubscript")
    else:
        start = int(first) - 1
        growth, sigma, temperature = growth[start:], sigma[start:], temperature[start:]
    sigma[np.abs(sigma) < 1e-15] = 1e-15
    count = int(growth.size)
    updated = dict(params)
    if count < float(min_num_adapt):
        return updated, count
    R = float(updated["R"])

    def residual(n: float, log_k_0: float, E_A: float):
        # Preserve log(k0 * sigma.^n * exp(...)), including MATLAB's complex
        # power/log semantics. n*log(sigma) is not equivalent for negative
        # sigma and changes both domain failures and underflow behavior.
        with np.errstate(all="ignore"):
            powers = (np.fromiter((real_power(value, n) for value in sigma), dtype=float, count=sigma.size)
                      if not np.any(sigma < 0) else np.emath.power(sigma, n))
            prediction = np.exp(log_k_0) * powers * np.exp(-E_A / R / temperature)
            return np.sum((np.emath.log(prediction) - np.log(growth)) ** 2)

    if adaptive_mode in {"E_A", "E_A_and_k_0", "E_A_and_n", "all"}:
        current = float(updated["E_A"])
        updated["E_A"] = abs(
            _bounded_minimum(
                lambda value: residual(
                    float(updated["n"]), _source_log(float(updated["k_0"])), value
                ),
                current / 2.0,
                current * 2.0,
                initial=current,
            )
        )

    if adaptive_mode in {"k_0", "E_A_and_k_0", "k_0_and_n", "all"}:
        current = _source_log(float(updated["k_0"]))
        optimum = _bounded_minimum(
            lambda value: residual(
                float(updated["n"]), value, float(updated["E_A"])
            ),
            current / 2.0,
            current * 2.0,
            initial=current,
        )
        updated["k_0"] = _source_exp(optimum)

    if adaptive_mode in {"n", "E_A_and_n", "k_0_and_n", "all"}:
        current = float(updated["n"])
        updated["n"] = abs(
            _bounded_minimum(
                lambda value: residual(
                    value, _source_log(float(updated["k_0"])), float(updated["E_A"])
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
