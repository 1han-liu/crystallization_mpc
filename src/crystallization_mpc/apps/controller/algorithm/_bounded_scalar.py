"""Private scalar bound-constrained compatibility solver for growth fitting.

This is a scalar primal-dual implementation, not a general replacement for
fmincon. It uses the complete symmetric Newton system for the state, two bound
slacks and their multipliers, forward differences, damped BFGS, and separate
primal/dual boundary steps. Monotone barrier reduction follows the published
interior-point algorithm:
https://www.mathworks.com/help/optim/ug/constrained-nonlinear-optimization-algorithms.html

No MATLAB runtime, reference fixtures, or test-case answers are used here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

import numpy as np

from ._numeric_backend import symmetric_solve
from ._optimizer_bounds import prepare_fmincon_start
from ._scalar_ldl import scalar_newton_solve
from crystallization_mpc.apps.controller.process import MATLAB_EMPTY, MatlabSpecialValue


@dataclass(frozen=True)
class ScalarBoundedResult:
    x: float
    fun: float | MatlabSpecialValue
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

    Bound preparation includes fixed variables and infeasible-return behavior.
    Nonfinite *trial* objectives are rejected by the line search; invalid
    initial objectives or derivatives are diagnosed by the adaptation boundary
    before this call.
    A finite last iterate is returned on a stopping/budget condition, matching
    the reference caller's use of x without a separate success-flag gate.
    """
    start = prepare_fmincon_start(lower, upper, initial)
    lower, upper, initial = start.lower, start.upper, start.initial
    if start.infeasible:
        return ScalarBoundedResult(float(initial), MATLAB_EMPTY, 0, 0,
                                   "The lower bound exceeds the upper bound.")
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
        if not lower <= value + delta <= upper and lower <= value - delta <= upper:
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
    if lower == upper:
        return ScalarBoundedResult(x, f, 0, evaluations, "All variables are fixed by bounds.")

    def bound_slacks(value):
        return np.array(([value - lower] if math.isfinite(lower) else [])
                        + ([upper - value] if math.isfinite(upper) else []))

    slack = bound_slacks(x)
    jacobian = np.array(([1.0] if math.isfinite(lower) else [])
                        + ([-1.0] if math.isfinite(upper) else []))
    count_bounds = slack.size
    # Keep unbiased, nonnegative-centered and positive working multipliers
    # distinct. The first barrier test precedes the working positivity repair;
    # the Newton system uses the repaired estimate. Reporting retains the
    # estimate with the smaller first-order measure at the current point.
    multiplier_matrix = np.outer(jacobian, jacobian) + np.diag(slack * slack)
    least = symmetric_solve(multiplier_matrix, g * jacobian) if count_bounds else np.empty(0)
    centered = (symmetric_solve(multiplier_matrix, g * jacobian + 0.1 * slack)
                if count_bounds else np.empty(0))
    multipliers = np.maximum(0.1, np.where(centered < 0, 0.1 / slack, centered))
    barrier, hessian = 0.1, 1.0
    last_barrier_update = 0

    def result(iteration: int, message: str) -> ScalarBoundedResult:
        return ScalarBoundedResult(float(x), float(f), iteration, evaluations, message)

    def first_order(estimate):
        return max(abs(g - estimate @ jacobian), np.max(np.minimum(np.minimum(abs(slack * estimate), abs(slack)), abs(estimate)), initial=0.0))
    unbiased = np.maximum(0.0, least)
    reported = unbiased.copy() if first_order(unbiased) < first_order(multipliers) else multipliers.copy()
    for iteration in range(max_iterations + 1):
        if first_order(multipliers) < first_order(reported):
            reported = multipliers.copy()
        tested = reported
        stationarity = abs(g - tested @ jacobian)
        optimality = first_order(tested)
        scale = max(1.0, float(np.max(np.abs(tested), initial=0.0)))
        if optimality <= 1e-6 * scale:
            return result(iteration, "Relative first-order optimality tolerance reached.")
        if iteration == max_iterations or evaluations + 2 > max_evaluations:
            return result(iteration, "Scalar fitting iteration/evaluation limit reached.")

        barrier_tested = np.maximum(0.0, centered) if iteration == 0 else multipliers
        barrier_error = max(abs(g - barrier_tested @ jacobian), np.max(np.abs(slack * barrier_tested - barrier), initial=0.0))
        while barrier_error <= barrier * scale:
            # Only one or two completed iterations use the fast update;
            # three completed iterations belong to the slower reduction.
            reduction = 0.01 if iteration - last_barrier_update < 3 else 0.2
            barrier = max(1e-8, reduction * barrier)
            last_barrier_update = iteration
            # A converged barrier subproblem can admit more than one barrier
            # reduction before another Newton step.  The initial reduction
            # is separate from that test, and the floor ends the reduction.
            if iteration == 0 or barrier == 1e-8:
                break
            barrier_error = max(abs(g - multipliers @ jacobian), np.max(np.abs(slack * multipliers - barrier), initial=0.0))

        # Do not analytically eliminate ds and substitute ds=J*dx afterward.
        # The source direct Newton solve returns all five components; replacing
        # its rounded slack directions changes near-bound finite differences
        # and can change the default stopping iteration.
        if count_bounds == 2:
            matrix = np.array([
                [hessian, 0., 0., -1., 1.],
                [0., multipliers[0] / slack[0], 0., 1., 0.],
                [0., 0., multipliers[1] / slack[1], 0., 1.],
                [-1., 1., 0., 0., 0.],
                [1., 0., 1., 0., 0.],
            ])
            rhs = np.array([-g + multipliers[0] - multipliers[1],
                            barrier / slack[0] - multipliers[0],
                            barrier / slack[1] - multipliers[1], 0., 0.])
        else:
            matrix = np.block([
                [np.array([[hessian]]), np.zeros((1, count_bounds)), -jacobian[None, :]],
                [np.zeros((count_bounds, 1)), np.diag(multipliers / slack), np.eye(count_bounds)],
                [-jacobian[:, None], np.eye(count_bounds), np.zeros((count_bounds, count_bounds))],
            ])
            rhs = np.r_[-g + multipliers @ jacobian, barrier / slack - multipliers,
                        np.zeros(count_bounds)]
        direction = scalar_newton_solve(matrix, rhs)
        dx, ds, dz = direction[0], direction[1:1 + count_bounds], direction[1 + count_bounds:]
        primal_step = 1.0
        dual_step = 1.0
        for value, delta in zip(slack, ds):
            if delta < 0:
                primal_step = min(primal_step, -0.995 * value / delta)
        for value, delta in zip(multipliers, dz):
            if delta < 0:
                dual_step = min(dual_step, -0.995 * value / delta)

        initial_primal_step = primal_step
        merit = f - barrier * np.sum(np.log(slack))
        merit_derivative = g * dx - barrier * np.sum(ds / slack)
        accepted = False
        for _ in range(60):
            if evaluations + 2 > max_evaluations:
                return result(iteration, "Scalar fitting evaluation limit reached.")
            new_x = x + primal_step * dx
            new_slack = bound_slacks(new_x)
            if np.all(new_slack > 0):
                new_f = evaluate(new_x)
                new_merit = new_f - barrier * np.sum(np.log(new_slack))
                # Sufficient decrease uses the primal quadratic model,
                # including slack curvature, rather than only g.T @ dx.
                model_change = merit_derivative + 0.5 * (
                    hessian * dx * dx + np.sum((multipliers / slack) * ds * ds))
                if math.isfinite(new_f) and new_merit <= merit + 1e-8 * primal_step * model_change:
                    accepted = True
                    break
            primal_step *= 0.5
        if not accepted:
            return result(iteration + 1, "Relative step tolerance reached.")

        small_step = abs(primal_step * dx) <= 1e-10 * max(1.0, abs(x))
        new_g = derivative(new_x, new_f)
        # Keep the accepted displacement: subtracting the rounded iterates
        # loses low bits which the reference BFGS update still retains.
        step = primal_step * dx
        gradient_change = new_g - g
        curvature = step * gradient_change
        predicted_curvature = hessian * step * step
        if curvature < 0.2 * predicted_curvature:
            damping = 0.8 * predicted_curvature / (predicted_curvature - curvature)
            gradient_change = damping * gradient_change + (1.0 - damping) * hessian * step
        if step * gradient_change > 0:
            hessian = (hessian
                       - (hessian * step) * (hessian * step) / predicted_curvature
                       + gradient_change * gradient_change / (step * gradient_change))
        # The line search shortens the *whole* primal-dual direction.  Taking
        # a full dual step after accepting only a fraction of the primal step
        # prematurely satisfies complementarity and reduces the barrier.  It
        # changes the returned iterate for the nearly flat log-k0 fits.
        multipliers += dual_step * (primal_step / initial_primal_step) * dz
        slack, x, f, g = new_slack, new_x, new_f, new_g
        if small_step:
            return result(iteration + 1, "Relative step tolerance reached.")

    raise AssertionError("Scalar iteration budget was not enforced.")
