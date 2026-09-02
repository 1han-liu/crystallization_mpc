"""Parameter adapters for gsensor detection routines."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping


PARAMS_G_KEYS = (
    "width",
    "ratio",
    "delta_theta",
    "width_divider",
    "num_peak",
    "len_min",
)
OPTIONAL_PARAMS_G_KEYS = (
    "hough_threshold",
    "hough_min_line_length",
    "hough_max_line_gap",
    "hough_rho_resolution",
    "hough_theta_resolution_deg",
    "hough_max_candidates",
)


def build_params_G(params: Mapping[str, Any]) -> SimpleNamespace:
    values: dict[str, Any] = {}
    for key in PARAMS_G_KEYS:
        flat_key = f"params_G.{key}"
        if flat_key not in params:
            raise KeyError(f"Missing detection parameter: {flat_key}")
        values[key] = params[flat_key]
    for key in OPTIONAL_PARAMS_G_KEYS:
        flat_key = f"params_G.{key}"
        if flat_key in params:
            values[key] = params[flat_key]
    return SimpleNamespace(**values)


__all__ = ["OPTIONAL_PARAMS_G_KEYS", "PARAMS_G_KEYS", "build_params_G"]
