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
    reference_2d = np.vstack(
        [
            _footprint_point(M),
            _footprint_point(W),
            _footprint_point(U),
            _footprint_point(V),
        ]
    )
    return {
        "type": "patch",
        "vertices": vertices.tolist(),
        "faces": [[1, 2, 3], [1, 2, 4], [1, 3, 4], [2, 3, 4]],
        "face_vertex_cdata": [1.0, 0.0, 0.0, 0.0],
        "face_color": "flat",
        "face_alpha": 0.1,
        "reference_2d": {
            "type": "footprint",
            "vertices": reference_2d.tolist(),
            "edges": [[1, 2], [1, 3], [1, 4], [2, 3], [2, 4], [3, 4]],
            "labels": ["M", "W", "U", "V"],
        },
    }


def _shown_point(point: Any) -> np.ndarray:
    point = np.asarray(point, dtype=float).reshape(-1)
    if point.size < 3:
        point = np.pad(point, (0, 3 - point.size))
    shown = point[:3].copy()
    shown[2] = -shown[2]
    return shown


def _footprint_point(point: Any) -> np.ndarray:
    shown = _shown_point(point)
    shown[2] = 0.0
    return shown


__all__ = ["show_3d"]
