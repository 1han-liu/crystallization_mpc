"""Reference Dormand--Prince steps, with explicit integration failures.

Healthy steps retain the reference arithmetic and interval-based maximum step.
Unlike the original GUI's ``x(end,:)`` behavior, an unfinished interval or a
nonfinite stage is an error, never a successful final state for the controller.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from ._fdlibm import real_power
from .._recovery import NumericalRecoveryError


class IntegrationIncompleteError(NumericalRecoveryError):
    """The transition did not produce a valid state at the requested time."""

    def __init__(self, reason: str, *, requested_dt: float, reached_time: float):
        self.reason = reason
        self.requested_dt = float(requested_dt)
        self.reached_time = float(reached_time)
        self.completed = False
        super().__init__(
            f"Integration incomplete ({reason}): reached {reached_time:.17g} s "
            f"of requested {requested_dt:.17g} s.",
            module="main", stage="state_transition",
        )


def ode45_final_ieee(function: Callable[[float, np.ndarray], np.ndarray],
                     initial: np.ndarray, duration: float, *, rtol: float,
                     atol: float, first_derivative: np.ndarray | None = None) -> np.ndarray:
    finish = float(duration)
    if np.iscomplexobj(initial):
        raise IntegrationIncompleteError("complex_initial_state", requested_dt=finish, reached_time=0.0)
    y = np.asarray(initial, dtype=float).copy()
    if not np.isfinite(finish):
        raise IntegrationIncompleteError("nonfinite_interval", requested_dt=finish, reached_time=0.0)
    if not np.isfinite(y).all():
        raise IntegrationIncompleteError("nonfinite_initial_state", requested_dt=finish, reached_time=0.0)
    if not (np.isfinite(rtol) and np.isfinite(atol) and rtol > 0 and atol > 0):
        raise ValueError("Integration tolerances must be finite and positive.")
    direction = -1.0 if finish < 0.0 else 1.0
    if finish == 0.0:
        raise ValueError("The integration interval must have distinct endpoints.")
    t = 0.0

    def evaluate(at: float, state: np.ndarray) -> np.ndarray:
        if not np.isfinite(state).all():
            raise IntegrationIncompleteError("nonfinite_stage", requested_dt=finish, reached_time=t)
        derivative = np.asarray(function(at, state))
        if (np.iscomplexobj(derivative) or derivative.shape != y.shape
                or not np.isfinite(derivative).all()):
            raise IntegrationIncompleteError("invalid_derivative", requested_dt=finish, reached_time=t)
        return derivative

    threshold = float(atol) / float(rtol)
    maximum_step = max(0.1 * abs(finish), 16.0 * np.finfo(float).eps * abs(finish))
    f = evaluate(t, y) if first_derivative is None else np.asarray(first_derivative)
    if np.iscomplexobj(f) or f.shape != y.shape or not np.isfinite(f).all():
        raise IntegrationIncompleteError("invalid_initial_derivative", requested_dt=finish, reached_time=t)
    with np.errstate(all="ignore"):
        relative = np.linalg.norm(f / np.fmax(np.abs(y), threshold), np.inf) / (.8 * real_power(rtol, .2))
        step = maximum_step
        if step * relative > 1:
            step = 1.0 / relative
        step = float(np.fmax(step, 16 * abs(np.spacing(t))))
        while direction * (finish - t) > 0:
            minimum_step = 16 * abs(np.spacing(t))
            current_maximum_step = max(minimum_step, maximum_step)
            step = float(np.fmin(current_maximum_step, np.fmax(minimum_step, step)))
            final_step = 1.1 * step >= abs(finish - t)
            if final_step:
                step = abs(finish - t)
            no_rejection = True
            while True:
                h = direction * step
                if not np.isfinite(h) or t + h == t:
                    raise IntegrationIncompleteError("time_not_advancing", requested_dt=finish, reached_time=t)
                k2 = evaluate(t + h*(1/5), y + h * ((1/5) * f))
                k3 = evaluate(t + h*(3/10), y + h * (3/40*f + 9/40*k2))
                k4 = evaluate(t + h*(4/5), y + h * (44/45*f - 56/15*k2 + 32/9*k3))
                k5 = evaluate(t + h*(8/9), y + h * (19372/6561*f - 25360/2187*k2 + 64448/6561*k3 - 212/729*k4))
                k6 = evaluate(t + h, y + h * (9017/3168*f - 355/33*k2 + 46732/5247*k3 + 49/176*k4 - 5103/18656*k5))
                next_t = finish if final_step else t + h
                h = next_t - t
                next_y = y + h * (35/384*f + 500/1113*k3 + 125/192*k4 - 2187/6784*k5 + 11/84*k6)
                k7 = evaluate(next_t, next_y)
                error_vector = 71/57600*f - 71/16695*k3 + 71/1920*k4 - 17253/339200*k5 + 22/525*k6 - 1/40*k7
                scale = np.fmax(np.fmax(np.abs(y), np.abs(next_y)), threshold)
                # ode45 keeps absh for its error scale after purifying the
                # signed h=tnew-t used in the final stage calculation.
                error = step * np.linalg.norm(error_vector / scale, np.inf)
                if not np.isfinite(error):
                    raise IntegrationIncompleteError("nonfinite_error_estimate", requested_dt=finish, reached_time=t)
                if error > rtol:
                    if step <= minimum_step:
                        raise IntegrationIncompleteError("minimum_step", requested_dt=finish, reached_time=t)
                    if no_rejection:
                        no_rejection = False
                        step = float(np.fmax(minimum_step, step * np.fmax(.1, .8 * real_power(rtol/error, .2))))
                    else:
                        step = float(np.fmax(minimum_step, .5 * step))
                    final_step = False
                    continue
                break
            if final_step:
                return next_y
            if no_rejection:
                multiplier = 1.25 * real_power(error/rtol, .2)
                step = step / multiplier if multiplier > .2 else 5 * step
            t, y, f = next_t, next_y, k7
    if t != finish:
        raise IntegrationIncompleteError("interval_not_completed", requested_dt=finish, reached_time=t)
    return y
