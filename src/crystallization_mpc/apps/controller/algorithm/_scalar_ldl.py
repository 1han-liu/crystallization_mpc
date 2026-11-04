"""Sparse-order LDL solve for scalar bound-constrained Newton systems.

The constraint graph is a path. Preserve its block and substitution order;
a dense factorization of the same matrix is a different rounded calculation.
This is not a general multidimensional sparse LDL implementation.
"""
from __future__ import annotations
import numpy as np


def scalar_newton_solve(matrix: np.ndarray, rhs: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=float)
    n = len(rhs)
    if n == 1:
        return np.asarray(rhs, dtype=float) / matrix[0, 0]
    adjacency = matrix != 0
    np.fill_diagonal(adjacency, False)
    leaves = np.flatnonzero(adjacency.sum(axis=0) == 1)
    order = [int(leaves[-1])]
    while len(order) < n:
        order.append(next(int(i) for i in np.flatnonzero(adjacency[order[-1]]) if i not in order))
    order = np.array(order)
    value = matrix[np.ix_(order, order)]
    a, t, b, h = value[0, 0], value[0, 1], value[1, 2], value[2, 2]
    lower = np.eye(n)
    diagonal = np.zeros((n, n))
    if n == 3 and abs(a) >= 0.5 * abs(t):
        diagonal[0, 0] = a
        lower[1, 0] = t / a
        diagonal[1, 1] = -lower[1, 0] * t
        lower[2, 1] = b / diagonal[1, 1]
        diagonal[2, 2] = h - lower[2, 1] * b
        block = False
    else:
        diagonal[:2, :2] = [[a, t], [t, 0.0]]
        lower[2, 0] = b / t
        lower[2, 1] = -a * b / (t * t)
        schur = h + a * (b / t) * (b / t)
        diagonal[2, 2] = schur
        block = True
    if n == 5:
        c, d, e = value[2, 3], value[3, 4], value[4, 4]
        lower[3, 2] = c / schur
        diagonal[3, 3] = -c * c / schur
        lower[4, 3] = d / diagonal[3, 3]
        diagonal[4, 4] = e + schur * (d / c) * (d / c)
    value = np.array(rhs, dtype=float)[order].copy()
    for column in range(n):
        for row in range(column + 1, n):
            if lower[row, column] != 0:
                value[row] -= lower[row, column] * value[column]
    solved = value.copy()
    first_diagonal = 0
    if block:
        solved[0] = value[1] / diagonal[0, 1]
        solved[1] = (value[0] - diagonal[0, 0] * solved[0]) / diagonal[0, 1]
        first_diagonal = 2
    for row in range(first_diagonal, n):
        solved[row] /= diagonal[row, row]
    for column in range(n - 1, -1, -1):
        for row in range(column + 1, n):
            if lower[row, column] != 0:
                solved[column] -= lower[row, column] * solved[row]
    result = np.empty_like(solved)
    result[order] = solved
    return result
