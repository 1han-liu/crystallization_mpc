"""Translation of gsensor/utils/show_3d.m for Web payload rendering."""

from __future__ import annotations

from typing import Any

import numpy as np


def show_3d(I: Any, M: Any, W: Any, U: Any, V: Any) -> dict[str, Any]:
    _ = I
    vertices = np.vstack(
        [
            _shown_point(M),
            _shown_point(W),
            _shown_point(U),
            _shown_point(V),
        ]
    )
    return {
        "type": "patch",
        "vertices": vertices.tolist(),
        "faces": [[1, 2, 3], [1, 2, 4], [1, 3, 4], [2, 3, 4]],
        "face_vertex_cdata": [1.0, 0.0, 0.0, 0.0],
        "face_color": "flat",
        "face_alpha": 0.1,
    }


def _shown_point(point: Any) -> np.ndarray:
    point = np.asarray(point, dtype=float).reshape(-1)
    if point.size < 3:
        point = np.pad(point, (0, 3 - point.size))
    shown = point[:3].copy()
    shown[2] = -shown[2]
    return shown


__all__ = ["show_3d"]
