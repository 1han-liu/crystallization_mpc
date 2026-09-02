"""Seed generation and crystallization mass-balance equations."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .thermodynamics import calc_G


def calc_volume(size: Any) -> np.ndarray | float:
    value = np.pi * np.asarray(size, dtype=float) ** 3 / 6.0
    return float(value) if value.ndim == 0 else value


def calc_surface_area(size: Any) -> np.ndarray | float:
    value = np.pi * np.asarray(size, dtype=float) ** 2
    return float(value) if value.ndim == 0 else value


def calc_size_from_volume(volume: Any) -> np.ndarray | float:
    value = np.cbrt(6.0 * np.asarray(volume, dtype=float) / np.pi)
    return float(value) if value.ndim == 0 else value


def calc_next_crystallization_mass_balance(
    params: Mapping[str, Any],
    m_solute: float,
    m_solvent: float,
    size_list: np.ndarray,
    T: float,
    dt: float,
) -> tuple[float, np.ndarray]:
    if m_solvent <= 0:
        raise ValueError("m_solvent must be positive.")
    sizes = np.asarray(size_list, dtype=float).reshape(-1)
    if not np.isfinite(sizes).all() or np.any(sizes < 0):
        raise ValueError("size_list must contain finite, nonnegative sizes.")
    concentration = float(m_solute) / float(m_solvent)
    growth = float(calc_G(params, concentration, float(T), is_model=True))
    surface_area = np.asarray(calc_surface_area(sizes), dtype=float)
    volume = np.asarray(calc_volume(sizes), dtype=float)
    delta_volume = surface_area * growth * float(dt)
    m_solute_next = float(m_solute) - float(
        np.sum(delta_volume * float(params["rho_solute"]))
    )
    concentration_next = float(np.real(m_solute_next / float(m_solvent)))
    sizes_next = np.asarray(calc_size_from_volume(volume + delta_volume), dtype=float)
    return concentration_next, sizes_next


def create_size_list(
    params: Mapping[str, Any],
    mass_kg: float,
    mean_m: float,
    std_m: float,
    *,
    rng: np.random.Generator,
) -> np.ndarray:
    """Generate a deterministic Python simulation population with MATLAB mass semantics.

    MATLAB's `fminsearch` repeatedly draws random arrays during its objective,
    so its seed population is materialized as fixture data for parity tests.
    Production simulation uses the same normal size model and chooses the
    population count from expected spherical mass.
    """

    expected_volume = np.pi / 6.0 * (
        mean_m**3 + 3.0 * mean_m * std_m**2
    )
    count = max(1, int(round(float(mass_kg) / (float(params["rho_solute"]) * expected_volume))))
    sizes = rng.normal(float(mean_m), float(std_m), count)
    return np.maximum(sizes, np.finfo(float).eps)


__all__ = [
    "calc_next_crystallization_mass_balance",
    "calc_size_from_volume",
    "calc_surface_area",
    "calc_volume",
    "create_size_list",
]
