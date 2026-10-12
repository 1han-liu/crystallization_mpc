"""Immutable, observational Controller diagnostics (never recovery state)."""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, Mapping


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Diagnostics must contain finite numbers.")
        return value
    raise TypeError(f"Unsupported diagnostic value: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ControllerDiagnosticsSnapshot:
    """One committed cycle's copied scalar fields and full raw evidence."""

    tick_seq: int
    elapsed_s: float
    run_id: str
    scalar_values: Mapping[str, int | float | bool | str]
    raw_data: Mapping[str, Any]

    def __post_init__(self) -> None:
        scalars = dict(self.scalar_values)
        for name, value in scalars.items():
            if not name.startswith("diag_") or not isinstance(value, (int, float, bool, str)):
                raise ValueError("Diagnostics fields must be diag_ scalar values.")
        object.__setattr__(self, "scalar_values", _freeze(scalars))
        object.__setattr__(self, "raw_data", _freeze(self.raw_data))

    @classmethod
    def from_data(
        cls, *, tick_seq: int, elapsed_s: float, run_id: str,
        fields: Mapping[str, int | float | bool | str], raw: Mapping[str, Any],
    ) -> "ControllerDiagnosticsSnapshot":
        return cls(tick_seq, elapsed_s, run_id, fields, raw)

    def fields(self) -> dict[str, int | float | bool | str]:
        return dict(self.scalar_values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick_seq": self.tick_seq, "elapsed_s": self.elapsed_s,
            "run_id": self.run_id, "fields": self.fields(),
            "raw": _thaw(self.raw_data),
        }


__all__ = ["ControllerDiagnosticsSnapshot"]
