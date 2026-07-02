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


def build_params_G(params: Mapping[str, Any]) -> SimpleNamespace:
    values: dict[str, Any] = {}
    for key in PARAMS_G_KEYS:
        flat_key = f"params_G.{key}"
        if flat_key not in params:
            raise KeyError(f"Missing detection parameter: {flat_key}")
        values[key] = params[flat_key]
    return SimpleNamespace(**values)


__all__ = ["PARAMS_G_KEYS", "build_params_G"]
