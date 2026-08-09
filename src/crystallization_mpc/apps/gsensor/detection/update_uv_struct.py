"""Translation of gsensor/detection/update_uv_struct.m."""

from __future__ import annotations

from typing import Any

from crystallization_mpc.apps.gsensor.detection.update_line import update_line


def update_uv_struct(
    uv_struct,
    image_file,
    ii: int,
    params_G,
    kernel,
    *,
    debug_dir=None,
    debug_label: str | None = None,
):
    uv_struct.line, uv_struct.dist, I_orig = update_line(
        image_file,
        params_G,
        uv_struct.line,
        uv_struct.t,
        uv_struct.e,
        uv_struct.n,
        uv_struct.o,
        uv_struct.is_opposite,
        kernel,
        debug_dir=debug_dir,
        debug_label=debug_label,
    )
    _set_matlab_indexed_value(uv_struct, "dist_array", ii, uv_struct.dist)
    return uv_struct, I_orig


def _set_matlab_indexed_value(target: Any, attr: str, ii: int, value: Any) -> None:
    array = getattr(target, attr, None)
    if array is None:
        array = []
        setattr(target, attr, array)

    index = max(int(ii) - 1, 0)
    if isinstance(array, list):
        if len(array) <= index:
            array.extend([None] * (index + 1 - len(array)))
        array[index] = value
        return

    array[index] = value


__all__ = ["update_uv_struct"]
