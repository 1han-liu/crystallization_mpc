"""Translation of gsensor/detection/update_EKF_G.m."""

from __future__ import annotations

from typing import Any

import numpy as np


def update_EKF_G(uv_struct, dt_G, resolution, ii: int):
    _predict(uv_struct.EKF_G)

    measurement_G = np.zeros((3,), dtype=float)
    dist_array = getattr(uv_struct, "dist_array")
    index = _matlab_index(ii)

    measurement_G[0] = _array_value(dist_array, index) * resolution
    try:
        measurement_G[1] = (
            _array_value(dist_array, index) - _array_value(dist_array, index - 1)
        ) / dt_G * resolution
        measurement_G[2] = (
            _array_value(dist_array, index)
            + _array_value(dist_array, index - 2)
            - 2 * _array_value(dist_array, index - 1)
        ) / (dt_G**2) * resolution
    except Exception:
        pass

    _set_matlab_indexed_value(uv_struct, "distance_array", ii, measurement_G[0])
    _set_matlab_indexed_value(uv_struct, "G_array", ii, measurement_G[1])

    corrected = np.asarray(_correct(uv_struct.EKF_G, measurement_G), dtype=float).reshape(3)
    _set_column(uv_struct, "x_G_array", ii, corrected)
    _set_matlab_indexed_value(uv_struct, "distance_KF_array", ii, corrected[0])
    _set_matlab_indexed_value(uv_struct, "G_KF_array", ii, corrected[1])

    return uv_struct


def _predict(ekf: Any) -> Any:
    predict = getattr(ekf, "predict", None)
    if predict is None:
        raise AttributeError("uv_struct.EKF_G must provide predict().")
    return predict()


def _correct(ekf: Any, measurement: np.ndarray) -> Any:
    correct = getattr(ekf, "correct", None)
    if correct is None:
        raise AttributeError("uv_struct.EKF_G must provide correct(measurement).")
    return correct(measurement)


def _matlab_index(ii: int) -> int:
    return max(int(ii) - 1, 0)


def _array_value(array: Any, index: int) -> float:
    if index < 0:
        raise IndexError(index)
    return float(np.asarray(array, dtype=float).reshape(-1)[index])


def _set_matlab_indexed_value(target: Any, attr: str, ii: int, value: Any) -> None:
    array = getattr(target, attr, None)
    if array is None:
        array = []
        setattr(target, attr, array)

    index = _matlab_index(ii)
    if isinstance(array, list):
        if len(array) <= index:
            array.extend([None] * (index + 1 - len(array)))
        array[index] = float(value)
        return

    array[index] = value


def _set_column(target: Any, attr: str, ii: int, values: np.ndarray) -> None:
    array = getattr(target, attr, None)
    index = _matlab_index(ii)
    values = np.asarray(values, dtype=float).reshape(3)

    if array is None or (isinstance(array, list) and not array):
        array = np.zeros((3, index + 1), dtype=float)
        setattr(target, attr, array)
    elif isinstance(array, list):
        array = np.asarray(array, dtype=float)
        if array.size == 0:
            array = np.zeros((3, index + 1), dtype=float)
        elif array.ndim == 1:
            array = array.reshape(3, -1)
        setattr(target, attr, array)

    if array.shape[1] <= index:
        grown = np.zeros((3, index + 1), dtype=float)
        grown[:, : array.shape[1]] = array
        array = grown
        setattr(target, attr, array)
    array[:, index] = values


__all__ = ["update_EKF_G"]
