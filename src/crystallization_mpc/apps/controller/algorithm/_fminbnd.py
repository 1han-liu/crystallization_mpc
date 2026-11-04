"""Scalar golden-section/parabolic search with the source default decisions.

Only the returned scalar is consumed by the controller.  IEEE objective values
are deliberately compared as supplied: NaN is neither an artificial penalty nor
a reason to repeat a calculation using an old measurement.
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np
from crystallization_mpc.apps.controller.process import MATLAB_EMPTY, MatlabSpecialValue
from ._optimizer_bounds import ScalarOptimizerError


def fminbnd(function: Callable[[float], float], lower: float, upper: float,
            *, tolerance: float = 1e-4, max_evaluations: int = 500,
            max_iterations: int = 500) -> float | MatlabSpecialValue:
    a, b = float(lower), float(upper)
    if not np.isfinite(a) or not np.isfinite(b):
        raise ScalarOptimizerError("Bounds must be finite scalar doubles.", "MATLAB:fminbnd:InvalidBoundInput")
    if a > b:
        return MATLAB_EMPTY

    def evaluate(x):
        value = np.asarray(function(x))
        return value.reshape(-1)[0] if value.size == 1 else value
    golden = .5 * (3.0 - math.sqrt(5.0))
    seps = math.sqrt(np.finfo(float).eps)
    xf = v = w = a + golden * (b - a)
    d = e = 0.0
    fx = fv = fw = evaluate(xf)
    if np.size(fx) != 1:
        raise ScalarOptimizerError("The objective function must return a scalar value.",
                                   "MATLAB:fminbnd:NonScalarObj")
    midpoint = .5 * (a + b)
    tol1 = seps * abs(xf) + tolerance / 3.0
    tol2 = 2.0 * tol1
    count, iteration = 1, 0
    while abs(xf - midpoint) > tol2 - .5 * (b - a):
        use_golden = True
        if abs(e) > tol1:
            r = (xf - w) * (fx - fv)
            q = (xf - v) * (fx - fw)
            p = (xf - v) * q - (xf - w) * r
            q = 2.0 * (q - r)
            if q > 0:
                p = -p
            q = abs(q)
            r, e = e, d
            if abs(p) < abs(.5 * q * r) and p > q * (a - xf) and p < q * (b - xf):
                use_golden = False
                d = p / q
                trial = xf + d
                if trial - a < tol2 or b - trial < tol2:
                    d = tol1 * (1.0 if midpoint >= xf else -1.0)
        if use_golden:
            e = a - xf if xf >= midpoint else b - xf
            d = golden * e
        trial = xf + (1.0 if d >= 0 else -1.0) * max(abs(d), tol1)
        value = evaluate(trial)
        count += 1
        iteration += 1
        if value <= fx:
            if trial >= xf:
                a = xf
            else:
                b = xf
            v, fv, w, fw, xf, fx = w, fw, xf, fx, trial, value
        else:
            if trial < xf:
                a = trial
            else:
                b = trial
            if value <= fw or w == xf:
                v, fv, w, fw = w, fw, trial, value
            elif value <= fv or v == xf or v == w:
                v, fv = trial, value
        midpoint = .5 * (a + b)
        tol1 = seps * abs(xf) + tolerance / 3.0
        tol2 = 2.0 * tol1
        if count >= max_evaluations or iteration >= max_iterations:
            break
    return float(xf)
