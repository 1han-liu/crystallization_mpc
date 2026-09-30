"""Private scalar bound-constrained compatibility solver for growth fitting.

This is an independent scalar primal-dual implementation, not a general
replacement for fmincon. It eliminates the two bound slacks analytically and
uses forward differences, damped BFGS, and separate primal/dual boundary steps.
The monotone barrier reduction (1/100 after at most two inner iterations,
otherwise 1/5) follows MathWorks' published interior-point algorithm:
https://www.mathworks.com/help/optim/ug/constrained-nonlinear-optimization-algorithms.html

No MATLAB runtime, reference fixtures, or test-case answers are used here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class ScalarBoundedResult:
    x: float
    fun: float
    iterations: int
    evaluations: int
    message: str


def bounded_scalar(
    function: Callable[[float], float],
    lower: float,
    upper: float,
    *,
    initial: float,
    max_iterations: int = 100,
    max_evaluations: int = 3000,
) -> ScalarBoundedResult:
    """Minimize a scalar objective without discarding its supplied initial guess.

    Growth fits provide a strictly interior initial value. Nonfinite *trial*
    objectives are rejected by the line search; invalid initial values or
    derivatives are diagnosed by the adaptation boundary before this call.
    A finite last iterate is returned on a stopping/budget condition, matching
    the reference caller's use of x without a separate success-flag gate.
    """
    if not all(math.isfinite(v) for v in (lower, initial, upper)) or not lower < initial < upper:
        raise ValueError("Scalar fitting requires finite bounds and an interior initial value.")
    if max_iterations < 0 or max_evaluations < 2:
        raise ValueError("Invalid scalar fitting iteration/evaluation budget.")

    evaluations = 0

    def evaluate(value: float) -> float:
        nonlocal evaluations
        evaluations += 1
        return float(function(float(value)))

    def derivative(value: float, objective: float) -> float:
        delta = math.sqrt(np.finfo(float).eps) * max(abs(value), 1.0)
        if value < 0:
            delta = -delta
        if not lower <= value + delta <= upper:
            delta = -delta
        shifted = evaluate(value + delta)
        if not math.isfinite(shifted):
            raise ValueError("Scalar fitting finite-difference derivative is undefined.")
        return (shifted - objective) / delta

    x = float(initial)
    f = evaluate(x)
    if not math.isfinite(f):
        raise ValueError("Scalar fitting initial objective is undefined.")
    g = derivative(x, f)
    slack = np.array([x - lower, upper - x])
    jacobian = np.array([1.0, -1.0])
    # Least-squares multiplier estimate for the initial optimality check;
    # strictly positive working multipliers are used for the Newton system.
    least = g * jacobian / slack**2 / (1.0 + np.sum(1.0 / slack**2))
    multipliers = np.maximum(0.1, least)
    barrier, hessian = 0.1, 1.0
    last_barrier_update = 0

    def result(iteration: int, message: str) -> ScalarBoundedResult:
        return ScalarBoundedResult(float(x), float(f), iteration, evaluations, message)

    for iteration in range(max_iterations + 1):
        tested = np.maximum(0.0, least) if iteration == 0 else multipliers
        stationarity = abs(g - tested @ jacobian)
        optimality = max(stationarity, np.max(np.minimum(slack, tested)))
        scale = max(1.0, float(np.linalg.norm(tested, np.inf)))
        if optimality <= 1e-6 * scale:
            return result(iteration, "Relative first-order optimality tolerance reached.")
        if iteration == max_iterations or evaluations + 2 > max_evaluations:
            return result(iteration, "Scalar fitting iteration/evaluation limit reached.")

        barrier_error = max(stationarity, np.max(np.abs(slack * tested - barrier)))
        if barrier_error <= barrier * scale:
            reduction = 0.01 if iteration - last_barrier_update <= 2 else 0.2
            barrier = max(1e-8, reduction * barrier)
            last_barrier_update = iteration

        dx = (-g + barrier * np.sum(jacobian / slack)) / (
            hessian + np.sum(multipliers / slack)
        )
        ds = jacobian * dx
        dz = (barrier - slack * multipliers - multipliers * ds) / slack
        primal_step = 1.0
        dual_step = 1.0
        for value, delta in zip(slack, ds):
            if delta < 0:
                primal_step = min(primal_step, -0.995 * value / delta)
        for value, delta in zip(multipliers, dz):
            if delta < 0:
                dual_step = min(dual_step, -0.995 * value / delta)

        merit = f - barrier * np.sum(np.log(slack))
        merit_derivative = g * dx - barrier * np.sum(ds / slack)
        accepted = False
        for _ in range(60):
            if evaluations + 2 > max_evaluations:
                return result(iteration, "Scalar fitting evaluation limit reached.")
            new_x = x + primal_step * dx
            new_slack = np.array([new_x - lower, upper - new_x])
            if np.all(new_slack > 0):
                new_f = evaluate(new_x)
                new_merit = new_f - barrier * np.sum(np.log(new_slack))
                if math.isfinite(new_f) and new_merit <= merit + 1e-4 * primal_step * merit_derivative:
                    accepted = True
                    break
            primal_step *= 0.5
        if not accepted or abs(primal_step * dx) <= 1e-10 * max(1.0, abs(x)):
            return result(iteration + 1, "Relative step tolerance reached.")

        new_g = derivative(new_x, new_f)
        step = new_x - x
        gradient_change = new_g - g
        curvature = step * gradient_change
        predicted_curvature = hessian * step * step
        if curvature < 0.2 * predicted_curvature:
            damping = 0.8 * predicted_curvature / (predicted_curvature - curvature)
            gradient_change = damping * gradient_change + (1.0 - damping) * hessian * step
        if step * gradient_change > 0:
            hessian = gradient_change / step
        multipliers += dual_step * dz
        slack, x, f, g = new_slack, new_x, new_f, new_g

    raise AssertionError("Scalar iteration budget was not enforced.")
