"""Scalar bound preparation at the reference optimizer call boundary."""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np


class ScalarOptimizerError(ValueError):
    def __init__(self, message: str, identifier: str):
        super().__init__(message)
        self.reference_identifier = identifier


@dataclass(frozen=True)
class ScalarStart:
    lower: float | complex
    upper: float | complex
    initial: float | complex
    infeasible: bool


def prepare_fmincon_start(lower, upper, initial) -> ScalarStart:
    """Preserve validation, infeasible return, then interior-shift ordering.

    A reversed interval returns the supplied guess without evaluating the
    objective. Equal bounds fix the variable, but still admit the reference
    initial objective and forward-difference evaluations.
    """
    x = np.asarray(initial).item()
    lo, hi = np.asarray(lower).item(), np.asarray(upper).item()
    if np.imag(x) != 0:
        raise ScalarOptimizerError("Initial point contains complex values. Fmincon cannot continue.",
                                   "optimlib:fmincon:ComplexX0")
    x = float(np.real(x))
    if hi == -np.inf:
        raise ScalarOptimizerError("Upper bounds cannot contain -Inf.", "optimlib:checkbounds:MinusInfUb")
    if lo == np.inf:
        raise ScalarOptimizerError("Lower bounds cannot contain Inf.", "optimlib:checkbounds:PlusInfLb")
    if np.isnan(lo) or np.isnan(hi):
        raise ScalarOptimizerError("Bounds cannot contain NaN.", "optimlib:checkbounds:NanBounds")
    if np.real(lo) > np.real(hi):
        return ScalarStart(lo, hi, x, True)
    if lo == hi:
        x = lo
    else:
        distance = 0.45 * (hi - lo)
        push = distance if abs(distance) < 0.99 else 0.99
        if np.isfinite(lo) and np.real(x) <= np.real(lo):
            x = lo + push
        elif np.isfinite(hi) and np.real(x) >= np.real(hi):
            x = hi - push
    return ScalarStart(lo, hi, x, False)
