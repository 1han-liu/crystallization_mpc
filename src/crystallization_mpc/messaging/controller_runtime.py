"""Narrow, shared contract for operator changes within one running experiment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import CONTROLLER_ADAPTATION_MODES

RUNTIME_KEYS = frozenset({
    "control_target", "sigma_set", "G_set", "adaptation_enabled", "adaptation_mode",
})


def validate_runtime_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a patch without coercing bools, strings or unknown fields."""
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("Runtime changes must be a nonempty object.")
    if set(changes) - RUNTIME_KEYS:
        raise ValueError("Unsupported runtime setting.")
    result = dict(changes)
    if "control_target" in result and result["control_target"] not in ("sigma", "G"):
        raise ValueError("control_target must be sigma or G.")
    if "adaptation_mode" in result and result["adaptation_mode"] not in CONTROLLER_ADAPTATION_MODES:
        raise ValueError("Unsupported adaptation mode.")
    if "adaptation_enabled" in result and not isinstance(result["adaptation_enabled"], bool):
        raise ValueError("adaptation_enabled must be a boolean.")
    for key in ("sigma_set", "G_set"):
        if key in result:
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number.")
            try:
                value = float(value)
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"{key} must be finite and positive.") from exc
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be finite and positive.")
            result[key] = value
    return result


@dataclass(frozen=True)
class ControllerRuntimeUpdatePayload:
    run_id: str
    event_id: str
    expected_revision: int
    changes: Mapping[str, Any]
    requested_at: str

    def __post_init__(self) -> None:
        for key in ("run_id", "event_id", "requested_at"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be nonempty text.")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer.")
        object.__setattr__(self, "changes", validate_runtime_changes(self.changes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "event_id": self.event_id,
            "expected_revision": self.expected_revision, "changes": dict(self.changes),
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ControllerRuntimeUpdatePayload":
        if not isinstance(payload, Mapping):
            raise ValueError("Runtime command must be an object.")
        fields = {"run_id", "event_id", "expected_revision", "changes", "requested_at"}
        if set(payload) != fields:
            raise ValueError("Runtime command fields are missing or unsupported.")
        return cls(**dict(payload))
