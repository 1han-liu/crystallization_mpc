"""Finalize the Web-backed translation of gsensor/detection/initialize_DSCGR.m."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from scipy.io import savemat

from crystallization_mpc.apps.gsensor.initialization import (
    GsensorInitializationManager,
    InitializationSession,
)
from crystallization_mpc.apps.gsensor.morphs.calc_2d_3d_info import calc_2d_3d_info


def initialize_DSCGR(
    initialization: GsensorInitializationManager,
    session_id: str | None = None,
    output_dir: str | Path | None = None,
):
    session = initialization._get_session(session_id)
    if session.status != "ready_for_3d" or session.recovered_3d is None:
        raise ValueError("Complete Web initialization and select a 3D candidate first.")

    derived = initialization._derived(session)
    points = derived["points"]
    sides = derived["sides"]
    for key in ("m", "w", "u", "v"):
        if key not in points:
            raise ValueError(f"Initialization geometry is missing {key}.")

    kernel = _kernel_from_session(session)
    recovered = session.recovered_3d
    M = recovered["M"]
    W = recovered["W"]
    U = recovered["U"]
    V = recovered["V"]
    uv_struct_list = [None, None]
    uv_struct_list[0], uv_struct_list[1] = calc_2d_3d_info(
        M,
        W,
        U,
        V,
        sides.get("u_op"),
        sides.get("v_op"),
        sides.get("u_ad"),
        sides.get("v_ad"),
        session.is_full,
    )

    target_dir = Path("..") / "gsensor_data" if output_dir is None else Path(output_dir)
    if not target_dir.is_dir():
        target_dir.mkdir(parents=True)
    save_DSCGR_info(
        target_dir / "DSCGR_info.mat",
        {
            "image_file": _image_file_struct(session),
            "path": session.selected_image,
            "I": None,
            "is_full": session.is_full,
            "fig_2d": None,
            "ax_2d": None,
            "m": points["m"],
            "w": points["w"],
            "u": points["u"],
            "v": points["v"],
            "u_op_struct": sides.get("u_op"),
            "v_op_struct": sides.get("v_op"),
            "u_ad_struct": sides.get("u_ad"),
            "v_ad_struct": sides.get("v_ad"),
            "kernel": kernel,
            "corner": session.corner,
            "fig_3d": None,
            "ax_3d": None,
            "M": M,
            "W": W,
            "U": U,
            "V": V,
            "uv_struct_list": uv_struct_list,
        },
    )
    return uv_struct_list, kernel


def _kernel_from_session(session: InitializationSession) -> SimpleNamespace:
    k_c_cell = []
    k_o_cell = []
    for index in range(1, 5):
        corner_key = f"kernel.k_c_cell.{index}"
        outer_key = f"kernel.k_o_cell.{index}"
        if corner_key not in session.points or outer_key not in session.points:
            raise ValueError("Initialization kernel points are incomplete.")
        k_c_cell.append(_point_array(session.points[corner_key]))
        k_o_cell.append(_point_array(session.points[outer_key]))
    return SimpleNamespace(k_c_cell=k_c_cell, k_o_cell=k_o_cell)


def _image_file_struct(session: InitializationSession) -> SimpleNamespace:
    path = Path(session.selected_image)
    return SimpleNamespace(folder=str(path.parent), name=path.name)


def save_DSCGR_info(path: str | Path, variables: dict[str, Any]) -> None:
    savemat(str(path), {key: _mat_value(value) for key, value in variables.items()})


def _mat_value(value):
    if value is None:
        return np.array([])
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value
    if isinstance(value, dict):
        return {key: _mat_value(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {
            key: _mat_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [_mat_value(item) for item in value]
    return value


def _point_array(point: Any) -> np.ndarray:
    array = np.asarray(point, dtype=float).reshape(-1)
    if array.size == 2:
        array = np.array([array[0], array[1], 0.0])
    return array


__all__ = ["initialize_DSCGR", "save_DSCGR_info"]
