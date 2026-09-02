"""Glycine solubility, supersaturation, and growth-rate equations."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np


def _scalar_if_scalar(value: np.ndarray | np.floating) -> float | np.ndarray:
    array = np.asarray(value)
    return float(array) if array.ndim == 0 else array


def calc_c_meta(T: Any) -> float | np.ndarray:
    T_array = np.asarray(T, dtype=float)
    return _scalar_if_scalar(
        -0.0000072731749015 * T_array**2
        + 0.0094006235069230 * T_array
        - 1.7881779949842400
    )


def calc_c_sat(T: Any) -> float | np.ndarray:
    T_array = np.asarray(T, dtype=float)
    return _scalar_if_scalar(
        0.0000138423912271 * T_array**2
        - 0.0032814470982419 * T_array
        - 0.0047418316935379
    )


def calc_dc_sat_dT(T: Any) -> float | np.ndarray:
    T_array = np.asarray(T, dtype=float)
    return _scalar_if_scalar(2 * 0.0000138423912271 * T_array - 0.0032814470982419)


def calc_relative_sigma(c: Any, T: Any) -> float | np.ndarray:
    c_sat = np.asarray(calc_c_sat(T), dtype=float)
    value = (np.asarray(c, dtype=float) - c_sat) / c_sat
    return _scalar_if_scalar(value)


def calc_sigma(c: Any, T: Any) -> tuple[float | np.ndarray, float | np.ndarray]:
    c_sat = calc_c_sat(T)
    return calc_relative_sigma(c, T), c_sat


def calc_G(
    params: Mapping[str, Any], c: Any, T: Any, is_model: bool = False
) -> float | np.ndarray:
    suffix = "_proc" if is_model else ""
    n = float(params[f"n{suffix}"])
    E_A = float(params[f"E_A{suffix}"])
    k_0 = float(params[f"k_0{suffix}"])
    R = float(params["R"])
    T_array = np.asarray(T, dtype=float)
    sigma = np.asarray(calc_relative_sigma(c, T_array), dtype=float)
    value = k_0 * np.abs(sigma) ** n * np.exp(-E_A / (R * T_array)) * np.sign(sigma)
    return _scalar_if_scalar(value)


__all__ = [
    "calc_G",
    "calc_c_meta",
    "calc_c_sat",
    "calc_dc_sat_dT",
    "calc_relative_sigma",
    "calc_sigma",
]
