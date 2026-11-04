"""Seed generation and crystallization mass-balance equations."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ._fminsearch import fminsearch_scalar
from .thermodynamics import calc_G


def calc_volume(size: Any) -> np.ndarray | float:
    value = np.pi * np.asarray(size) ** 3 / 6.0
    return value.item() if value.ndim == 0 else value


def calc_surface_area(size: Any) -> np.ndarray | float:
    value = np.pi * np.asarray(size) ** 2
    return value.item() if value.ndim == 0 else value


def calc_size_from_volume(volume: Any) -> np.ndarray | float:
    value = np.emath.power(6.0 * np.asarray(volume) / np.pi, 1.0 / 3.0)
    return value.item() if value.ndim == 0 else value


def calc_next_crystallization_mass_balance(
    params: Mapping[str, Any],
    m_solute: float,
    m_solvent: float,
    size_list: np.ndarray,
    T: float,
    dt: float,
) -> tuple[float, np.ndarray]:
    sizes = np.asarray(size_list).reshape(-1)
    concentration = np.divide(m_solute, m_solvent)
    growth = float(calc_G(params, concentration, float(T), is_model=True))
    surface_area = np.asarray(calc_surface_area(sizes))
    volume = np.asarray(calc_volume(sizes))
    delta_volume = surface_area * growth * float(dt)
    m_solute_next = m_solute - np.sum(delta_volume * float(params["rho_solute"]))
    concentration_next = float(np.real(np.divide(m_solute_next, m_solvent)))
    sizes_next = np.asarray(calc_size_from_volume(volume + delta_volume))
    return concentration_next, sizes_next


def create_size_list(
    params: Mapping[str, Any],
    mass_kg: float,
    mean_m: float,
    std_m: float,
    *,
    rng,
) -> np.ndarray:
    """Search population count with a fresh random population on every call.

    The objective is intentionally stochastic, including draws for rejected
    simplex candidates.  A final independent draw creates the returned seeds.
    """
    def population(number):
        count = int(np.trunc(number))
        return float(mean_m) + float(std_m) * rng.standard_normal(count)

    def objective(number):
        sizes = population(number)
        return (np.sum(np.asarray(calc_volume(sizes)) * float(params["rho_solute"])) - float(mass_kg)) ** 2

    number = int(np.trunc(fminsearch_scalar(objective, 10000.0)))
    return population(number)


__all__ = [
    "calc_next_crystallization_mass_balance",
    "calc_size_from_volume",
    "calc_surface_area",
    "calc_volume",
    "create_size_list",
]
