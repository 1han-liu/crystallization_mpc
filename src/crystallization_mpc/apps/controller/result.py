"""Observational values returned after the source Controller statements."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping


@dataclass(frozen=True)
class ControllerStepResult:
    """A view of calculated values, without an extra numerical admission rule.

    The original loop carries its values to the subsequent G, display and
    write statements. This container must not reject or coerce them sooner.
    """

    valid: bool = True
    error: str | None = None
    T: float | None = None
    T_j: float | None = None
    c: float | None = None
    dT_dt: float | None = None
    dc_dt: float | None = None
    T_KF: float | None = None
    dT_dt_KF: float | None = None
    c_KF: float | None = None
    dc_dt_KF: float | None = None
    sigma: float | None = None
    G_model: float | None = None
    G_measure: float | None = None
    G_measure_KF: float | None = None
    target_value: float | None = None
    target_set: float | None = None
    target_error_abs: float | None = None
    dT_dt_set: float | None = None
    T_j_set: float | None = None
    objective: float | None = None
    E_A: float | None = None
    k_0: float | None = None
    n: float | None = None
    # Calculated lag in seconds and its multiplier, not the input parameters.
    # The MATLAB increase percentage is (t_lag_perc - 1) * 100.
    t_lag: float | None = None
    t_lag_perc: float | None = None

    NUMERIC_FIELDS: ClassVar[tuple[str, ...]] = (
        "T",
        "T_j",
        "c",
        "dT_dt",
        "dc_dt",
        "T_KF",
        "dT_dt_KF",
        "c_KF",
        "dc_dt_KF",
        "sigma",
        "G_model",
        "G_measure",
        "G_measure_KF",
        "target_value",
        "target_set",
        "target_error_abs",
        "dT_dt_set",
        "T_j_set",
        "objective",
        "E_A",
        "k_0",
        "n",
        "t_lag",
        "t_lag_perc",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.valid, bool):
            raise ValueError("Controller result valid must be a boolean.")
        if self.error is not None:
            if not isinstance(self.error, str):
                raise ValueError("Controller result error must be text or null.")
            if not self.error.strip():
                raise ValueError("Controller result error cannot be empty.")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "valid": self.valid,
            "error": str(self.error).strip() if self.error is not None else None,
        }
        result.update(
            {
                name: copy.deepcopy(getattr(self, name))
                for name in self.NUMERIC_FIELDS
            }
        )
        return result

    def fields(self) -> dict[str, Any]:
        """Return only values that may be persisted as InfluxDB fields."""

        document = self.to_dict()
        return {key: value for key, value in document.items() if value is not None}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ControllerStepResult":
        if not isinstance(value, Mapping):
            raise ValueError("Controller step result must be an object.")
        allowed = {"valid", "error", *cls.NUMERIC_FIELDS}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(
                f"Unknown Controller result field(s): {', '.join(unknown)}."
            )
        numeric = {name: copy.deepcopy(value.get(name)) for name in cls.NUMERIC_FIELDS}
        return cls(
            valid=value.get("valid", True),
            error=value.get("error"),
            **numeric,
        )


__all__ = ["ControllerStepResult"]
