"""Finalize the Web-backed translation of gsensor/detection/initialize_DSCGR.m."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np

from crystallization_mpc.apps.gsensor.initialization import (
    GsensorInitializationManager,
    InitializationSession,
)
from crystallization_mpc.apps.gsensor.morphs.calc_2d_3d_info import calc_2d_3d_info


def initialize_DSCGR(
    initialization: GsensorInitializationManager,
    session_id: str | None = None,
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


def _point_array(point: Any) -> np.ndarray:
    array = np.asarray(point, dtype=float).reshape(-1)
    if array.size == 2:
        array = np.array([array[0], array[1], 0.0])
    return array


__all__ = ["initialize_DSCGR"]
