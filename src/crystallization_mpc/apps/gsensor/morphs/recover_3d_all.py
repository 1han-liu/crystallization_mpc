"""Translation of gsensor/morphs/recover_3d_all.m candidate generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from crystallization_mpc.apps.gsensor.morphs.recover_3d import recover_3d
from crystallization_mpc.apps.gsensor.utils.show_3d import show_3d

NON_FULL_DIRECTIONS = (
    "in-1-1",
    "in-1-2",
    "in-2-1",
    "in-2-2",
    "in-3-1",
    "in-3-2",
    "in-4-1",
    "in-4-2",
    "out-1-1",
    "out-1-2",
    "out-2-1",
    "out-2-2",
    "out-3-1",
    "out-3-2",
    "out-4-1",
    "out-4-2",
)


@dataclass(frozen=True)
class Recover3DCandidate:
    choice: int
    direction: str
    label: str
    M: list[float]
    W: list[float]
    U: list[float]
    V: list[float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "choice": self.choice,
            "direction": self.direction,
            "label": self.label,
            "M": self.M,
            "W": self.W,
            "U": self.U,
            "V": self.V,
            "show_3d": show_3d(None, self.M, self.W, self.U, self.V),
        }


def recover_3d_all(I, m, w, u, v, corner, is_full):
    _ = I
    candidates = []
    if is_full:
        candidates.append(_candidate(m, w, u, v, corner, is_full, "outwards", 2))
    else:
        for choice, direction in enumerate(NON_FULL_DIRECTIONS, start=1):
            candidates.append(_candidate(m, w, u, v, corner, is_full, direction, choice))
    return candidates


def _candidate(m, w, u, v, corner, is_full, direction, choice):
    M, W, U, V = recover_3d(m, w, u, v, corner, is_full, direction)
    label = f"Corner points {direction}"
    return Recover3DCandidate(
        choice=choice,
        direction=direction,
        label=label,
        M=_point_list(M),
        W=_point_list(W),
        U=_point_list(U),
        V=_point_list(V),
    )


def _point_list(point) -> list[float]:
    point = np.asarray(point, dtype=float).reshape(-1)
    return [float(value) for value in point[:3]]


__all__ = ["NON_FULL_DIRECTIONS", "Recover3DCandidate", "recover_3d_all"]
