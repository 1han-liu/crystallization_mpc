"""Data model for one crystallization experiment session."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping


class ExperimentStatus(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    WAITING_FOR_INITIAL_IMAGE = "waiting_for_initial_image"
    INITIALIZING = "initializing"
    MEASURING = "measuring"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"


TERMINAL_EXPERIMENT_STATUSES = {
    ExperimentStatus.COMPLETED,
    ExperimentStatus.ERROR,
}

EXPERIMENT_STATUS_TRANSITIONS = {
    ExperimentStatus.CREATED: {
        ExperimentStatus.STARTING,
        ExperimentStatus.COMPLETED,
        ExperimentStatus.ERROR,
    },
    ExperimentStatus.STARTING: {
        ExperimentStatus.WAITING_FOR_INITIAL_IMAGE,
        ExperimentStatus.STOPPING,
        ExperimentStatus.ERROR,
    },
    ExperimentStatus.WAITING_FOR_INITIAL_IMAGE: {
        ExperimentStatus.INITIALIZING,
        ExperimentStatus.STOPPING,
        ExperimentStatus.ERROR,
    },
    ExperimentStatus.INITIALIZING: {
        ExperimentStatus.MEASURING,
        ExperimentStatus.STOPPING,
        ExperimentStatus.ERROR,
    },
    ExperimentStatus.MEASURING: {
        ExperimentStatus.STOPPING,
        ExperimentStatus.ERROR,
    },
    ExperimentStatus.STOPPING: {
        ExperimentStatus.COMPLETED,
        ExperimentStatus.ERROR,
    },
    ExperimentStatus.COMPLETED: set(),
    ExperimentStatus.ERROR: set(),
}


def require_experiment_transition(
    current: ExperimentStatus,
    target: ExperimentStatus,
) -> None:
    if target == current:
        return
    allowed = EXPERIMENT_STATUS_TRANSITIONS[current]
    if target not in allowed:
        raise ValueError(
            f"Cannot transition experiment from {current.value} to {target.value}."
        )


@dataclass(frozen=True)
class ExperimentManifest:
    run_id: str
    created_at: str
    status: ExperimentStatus = ExperimentStatus.CREATED
    schema_version: int = 1
    label: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    image_directory: str = "images"
    parameter_version: int | None = None
    params_snapshot_file: str | None = None
    gsensor_initialization_file: str | None = None
    error: str | None = None

    def updated(self, **changes: Any) -> "ExperimentManifest":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "label": self.label,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "image_directory": self.image_directory,
            "parameter_version": self.parameter_version,
            "params_snapshot_file": self.params_snapshot_file,
            "gsensor_initialization_file": self.gsensor_initialization_file,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentManifest":
        schema_version = int(data.get("schema_version", 1))
        if schema_version != 1:
            raise ValueError(f"Unsupported experiment manifest schema: {schema_version}")

        run_id = str(data.get("run_id", "")).strip()
        created_at = str(data.get("created_at", "")).strip()
        image_directory = str(data.get("image_directory", "images")).strip()
        if not run_id:
            raise ValueError("Experiment manifest is missing run_id.")
        if not created_at:
            raise ValueError("Experiment manifest is missing created_at.")
        if image_directory != "images":
            raise ValueError("Experiment image_directory must be 'images'.")

        status_value = str(data.get("status", ExperimentStatus.CREATED.value))
        # Phase 1 used "running" for every active experiment. Preserve existing
        # manifests by mapping that persisted value to the closest Phase 2 state.
        if status_value == "running":
            status_value = ExperimentStatus.MEASURING.value
        elif status_value == "failed":
            status_value = ExperimentStatus.ERROR.value

        return cls(
            schema_version=schema_version,
            run_id=run_id,
            label=_optional_string(data.get("label")),
            status=ExperimentStatus(status_value),
            created_at=created_at,
            started_at=_optional_string(data.get("started_at")),
            ended_at=_optional_string(data.get("ended_at")),
            image_directory=image_directory,
            parameter_version=_optional_int(data.get("parameter_version")),
            params_snapshot_file=_optional_string(data.get("params_snapshot_file")),
            gsensor_initialization_file=_optional_string(
                data.get("gsensor_initialization_file")
            ),
            error=_optional_string(data.get("error")),
        )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


__all__ = [
    "EXPERIMENT_STATUS_TRANSITIONS",
    "TERMINAL_EXPERIMENT_STATUSES",
    "ExperimentManifest",
    "ExperimentStatus",
    "require_experiment_transition",
]
