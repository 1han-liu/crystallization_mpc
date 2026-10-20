"""Run-scoped operator parameter patches, never startup snapshot replacements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .parameter_policy import validate_parameter_changes

PARAMETERS_UPDATE = "experiment.parameters.update"
PARAMETERS_PREPARE = "experiment.parameters.prepare"
PARAMETERS_COMMIT = "experiment.parameters.commit"
PARAMETERS_ABORT = "experiment.parameters.abort"
PARAMETERS_RESULT = "experiment.parameters.result"


@dataclass(frozen=True)
class RunParametersUpdatePayload:
    run_id: str
    event_id: str
    expected_revision: int
    changes: Mapping[str, Any]
    requested_at: str
    experiment_started_at: str

    def __post_init__(self) -> None:
        for key in ("run_id", "event_id", "requested_at", "experiment_started_at"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be nonempty text.")
        for key in ("requested_at", "experiment_started_at"):
            parsed = datetime.fromisoformat(getattr(self, key).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError(f"{key} must contain a timezone.")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer.")
        object.__setattr__(self, "changes", validate_parameter_changes(self.changes))

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "event_id": self.event_id,
                "expected_revision": self.expected_revision,
                "changes": {key: dict(values) for key, values in self.changes.items()},
                "requested_at": self.requested_at,
                "experiment_started_at": self.experiment_started_at}

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RunParametersUpdatePayload":
        fields = {"run_id", "event_id", "expected_revision", "changes", "requested_at",
                  "experiment_started_at"}
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError("Run parameter command fields are missing or unsupported.")
        return cls(**dict(payload))


def parse_runtime_command(payload: Mapping[str, Any]):
    """Keep existing five-setting commands wire-compatible."""
    if "experiment_started_at" in payload:
        return RunParametersUpdatePayload.from_mapping(payload)
    from .controller_runtime import ControllerRuntimeUpdatePayload
    return ControllerRuntimeUpdatePayload.from_mapping(payload)
