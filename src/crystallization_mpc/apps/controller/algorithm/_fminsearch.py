"""One-dimensional Nelder--Mead used for the source seed population search."""
from __future__ import annotations

import math
from typing import Callable

import numpy as np


def fminsearch_scalar(function: Callable[[float], float], initial: float,
                      *, tolerance_x: float = 1e-4, tolerance_f: float = 1e-4,
                      max_evaluations: int = 200, max_iterations: int = 200) -> float:
    x0 = float(initial)
    vertices = [x0, 1.05 * x0 if x0 != 0.0 else .00025]
    values = [function(vertices[0]), function(vertices[1])]
    evaluations, iterations = 2, 1

    def sort_simplex():
        nonlocal vertices, values
        # Stable ordering retains the first vertex on equal objective values.
        order = sorted(range(2), key=lambda i: (math.isnan(values[i]), values[i]))
        vertices = [vertices[i] for i in order]
        values = [values[i] for i in order]

    sort_simplex()
    while evaluations < max_evaluations and iterations < max_iterations:
        if (abs(values[0] - values[1]) <= max(tolerance_f, 10.0 * abs(np.spacing(values[0])))
                and abs(vertices[1] - vertices[0]) <= max(tolerance_x, 10.0 * abs(np.spacing(vertices[0])))):
            break
        best, worst = vertices
        reflected = 2.0 * best - worst
        reflected_value = function(reflected)
        evaluations += 1
        shrink = False
        if reflected_value < values[0]:
            expanded = 3.0 * best - 2.0 * worst
            expanded_value = function(expanded)
            evaluations += 1
            if expanded_value < reflected_value:
                vertices[1], values[1] = expanded, expanded_value
            else:
                vertices[1], values[1] = reflected, reflected_value
        elif reflected_value < values[1]:
            contracted = 1.5 * best - .5 * worst
            contracted_value = function(contracted)
            evaluations += 1
            if contracted_value <= reflected_value:
                vertices[1], values[1] = contracted, contracted_value
            else:
                shrink = True
        else:
            contracted = .5 * best + .5 * worst
            contracted_value = function(contracted)
            evaluations += 1
            if contracted_value < values[1]:
                vertices[1], values[1] = contracted, contracted_value
            else:
                shrink = True
        if shrink:
            vertices[1] = best + .5 * (vertices[1] - best)
            values[1] = function(vertices[1])
            evaluations += 1
        sort_simplex()
        iterations += 1
    return float(vertices[0])
