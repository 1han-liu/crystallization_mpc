"""Validated payload contracts shared by the three application services."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from crystallization_mpc.messaging.commands import EXPERIMENT_MODE_LIVE

GROWTH_RATE_UNIT = "m/s"
CONTROLLER_ADAPTATION_MODES = (
    "E_A",
    "k_0",
    "n",
    "E_A_and_k_0",
    "E_A_and_n",
    "k_0_and_n",
    "all",
)


class GrowthRateStatus(str, Enum):
    DISABLED = "disabled"
    WAITING_FOR_INITIAL_IMAGE = "waiting_for_initial_image"
    INITIALIZING = "initializing"
    BASELINE_READY = "baseline_ready"
    MEASURING = "measuring"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True)
class ExperimentStartPayload:
    run_id: str
    parameter_version: int
    started_at: str
    image_directory: str = "images"
    mode: str = EXPERIMENT_MODE_LIVE
    adaptation_enabled: bool = False
    adaptation_mode: str = "E_A"

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.started_at, "started_at")
        if int(self.parameter_version) < 1:
            raise ValueError("parameter_version must be greater than zero.")
        if self.image_directory != "images":
            raise ValueError("image_directory must be 'images'.")
        if self.mode != EXPERIMENT_MODE_LIVE:
            raise ValueError(f"mode must be {EXPERIMENT_MODE_LIVE!r}.")
        if not isinstance(self.adaptation_enabled, bool):
            raise ValueError("adaptation_enabled must be a boolean.")
        if self.adaptation_mode not in CONTROLLER_ADAPTATION_MODES:
            allowed = ", ".join(CONTROLLER_ADAPTATION_MODES)
            raise ValueError(f"adaptation_mode must be one of: {allowed}.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parameter_version": int(self.parameter_version),
            "started_at": self.started_at,
            "image_directory": self.image_directory,
            "mode": self.mode,
            "adaptation_enabled": self.adaptation_enabled,
            "adaptation_mode": self.adaptation_mode,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ExperimentStartPayload":
        return cls(
            run_id=_mapping_text(payload, "run_id"),
            parameter_version=_mapping_int(payload, "parameter_version"),
            started_at=_mapping_text(payload, "started_at"),
            image_directory=str(payload.get("image_directory", "images")),
            mode=str(payload.get("mode", EXPERIMENT_MODE_LIVE)),
            adaptation_enabled=(
                _mapping_bool(payload, "adaptation_enabled")
                if "adaptation_enabled" in payload
                else False
            ),
            adaptation_mode=str(payload.get("adaptation_mode", "E_A")),
        )


@dataclass(frozen=True)
class ExperimentStopPayload:
    run_id: str
    stopped_at: str
    reason: str = "central_stop"

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.stopped_at, "stopped_at")
        _required_text(self.reason, "reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stopped_at": self.stopped_at,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ExperimentStopPayload":
        return cls(
            run_id=_mapping_text(payload, "run_id"),
            stopped_at=_mapping_text(payload, "stopped_at"),
            reason=str(payload.get("reason", "central_stop")),
        )


@dataclass(frozen=True)
class GsensorActivationPayload:
    """One ordered operator request to enable or disable Gsensor."""

    run_id: str
    experiment_started_at: str
    event_id: str
    revision: int
    requested_at: str

    def __post_init__(self) -> None:
        _strict_required_text(self.run_id, "run_id")
        _strict_required_text(self.experiment_started_at, "experiment_started_at")
        _strict_required_text(self.event_id, "event_id")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer.")
        _strict_required_text(self.requested_at, "requested_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "experiment_started_at": self.experiment_started_at,
            "event_id": self.event_id,
            "revision": self.revision,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "GsensorActivationPayload":
        return cls(
            run_id=_mapping_strict_text(payload, "run_id"),
            experiment_started_at=_mapping_strict_text(
                payload,
                "experiment_started_at",
            ),
            event_id=_mapping_strict_text(payload, "event_id"),
            revision=_mapping_strict_positive_int(payload, "revision"),
            requested_at=_mapping_strict_text(payload, "requested_at"),
        )


@dataclass(frozen=True)
class ControllerAddSeedPayload:
    """One operator-confirmed seed-addition event for a running experiment."""

    run_id: str
    event_id: str
    added_at: str

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.event_id, "event_id")
        _required_text(self.added_at, "added_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_id": self.event_id,
            "added_at": self.added_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ControllerAddSeedPayload":
        return cls(
            run_id=_mapping_text(payload, "run_id"),
            event_id=_mapping_text(payload, "event_id"),
            added_at=_mapping_text(payload, "added_at"),
        )


@dataclass(frozen=True)
class ControllerAdaptationPayload:
    """One requested runtime change to Controller parameter adaptation."""

    run_id: str
    event_id: str
    enabled: bool
    mode: str
    requested_at: str

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.event_id, "event_id")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean.")
        if self.mode not in CONTROLLER_ADAPTATION_MODES:
            allowed = ", ".join(CONTROLLER_ADAPTATION_MODES)
            raise ValueError(f"mode must be one of: {allowed}.")
        _required_text(self.requested_at, "requested_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_id": self.event_id,
            "enabled": self.enabled,
            "mode": self.mode,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ControllerAdaptationPayload":
        return cls(
            run_id=_mapping_text(payload, "run_id"),
            event_id=_mapping_text(payload, "event_id"),
            enabled=_mapping_bool(payload, "enabled"),
            mode=_mapping_text(payload, "mode"),
            requested_at=_mapping_text(payload, "requested_at"),
        )


@dataclass(frozen=True)
class GrowthRateSamplePayload:
    run_id: str
    frame_seq: int
    image_name: str
    captured_at: str
    processed_at: str
    dt_s: float
    valid: bool
    status: str
    G_u: float | None
    G_u_KF: float | None
    G_v: float | None
    G_v_KF: float | None
    error: str | None = None
    unit: str = GROWTH_RATE_UNIT
    experiment_started_at: str | None = None
    control_revision: int | None = None
    initialization_generation: int | None = None
    alignment_revision: int | None = None

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.image_name, "image_name")
        _required_text(self.captured_at, "captured_at")
        _required_text(self.processed_at, "processed_at")
        _required_text(self.status, "status")
        if int(self.frame_seq) < 1:
            raise ValueError("frame_seq must start at 1; frame 0 is the baseline.")
        if not math.isfinite(float(self.dt_s)) or float(self.dt_s) <= 0:
            raise ValueError("dt_s must be a finite positive number.")
        if self.unit != GROWTH_RATE_UNIT:
            raise ValueError(f"unit must be {GROWTH_RATE_UNIT!r}.")

        if self.experiment_started_at is not None:
            _strict_required_text(self.experiment_started_at, "experiment_started_at")
        for name in ("control_revision", "initialization_generation", "alignment_revision"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or null.")

        values = (self.G_u, self.G_u_KF, self.G_v, self.G_v_KF)
        if self.valid:
            if any(value is None for value in values):
                raise ValueError("A valid growth-rate sample requires all four G values.")
            if not all(math.isfinite(float(value)) for value in values if value is not None):
                raise ValueError("Growth-rate values must be finite.")
            if self.error is not None:
                raise ValueError("A valid growth-rate sample cannot include an error.")
        else:
            if any(value is not None for value in values):
                raise ValueError("An invalid growth-rate sample must use null G values.")
            _required_text(self.error, "error")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "frame_seq": int(self.frame_seq),
            "image_name": self.image_name,
            "captured_at": self.captured_at,
            "processed_at": self.processed_at,
            "dt_s": float(self.dt_s),
            "unit": self.unit,
            "valid": bool(self.valid),
            "status": self.status,
            "G_u": self.G_u,
            "G_u_KF": self.G_u_KF,
            "G_v": self.G_v,
            "G_v_KF": self.G_v_KF,
            "error": self.error,
        }
        for name in ("experiment_started_at", "control_revision", "initialization_generation", "alignment_revision"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "GrowthRateSamplePayload":
        return cls(
            run_id=_mapping_text(payload, "run_id"),
            frame_seq=_mapping_int(payload, "frame_seq"),
            image_name=_mapping_text(payload, "image_name"),
            captured_at=_mapping_text(payload, "captured_at"),
            processed_at=_mapping_text(payload, "processed_at"),
            dt_s=_mapping_float(payload, "dt_s"),
            unit=str(payload.get("unit", GROWTH_RATE_UNIT)),
            valid=_mapping_bool(payload, "valid"),
            status=_mapping_text(payload, "status"),
            G_u=_optional_float(payload.get("G_u")),
            G_u_KF=_optional_float(payload.get("G_u_KF")),
            G_v=_optional_float(payload.get("G_v")),
            G_v_KF=_optional_float(payload.get("G_v_KF")),
            error=_optional_text(payload.get("error")),
            experiment_started_at=_mapping_optional_strict_text(payload, "experiment_started_at"),
            control_revision=_mapping_optional_nonnegative_int(payload, "control_revision"),
            initialization_generation=_mapping_optional_nonnegative_int(payload, "initialization_generation"),
            alignment_revision=_mapping_optional_nonnegative_int(payload, "alignment_revision"),
        )


@dataclass(frozen=True)
class GrowthRateStatusPayload:
    run_id: str
    status: GrowthRateStatus
    occurred_at: str
    frame_seq: int | None = None
    image_name: str | None = None
    error: str | None = None
    enabled: bool | None = None
    control_revision: int | None = None
    experiment_started_at: str | None = None
    control_event_id: str | None = None
    initialization_generation: int | None = None
    alignment_revision: int | None = None
    measurement_ready: bool | None = None

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.occurred_at, "occurred_at")
        if self.frame_seq is not None and int(self.frame_seq) < 0:
            raise ValueError("frame_seq cannot be negative.")
        if self.status == GrowthRateStatus.ERROR:
            _required_text(self.error, "error")
        elif self.error is not None:
            raise ValueError("Only error status may include an error message.")
        if self.enabled is not None and not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean or null.")
        if self.control_revision is not None and (
            type(self.control_revision) is not int or self.control_revision < 0
        ):
            raise ValueError("control_revision must be a nonnegative integer or null.")
        if self.experiment_started_at is not None:
            _strict_required_text(
                self.experiment_started_at,
                "experiment_started_at",
            )
        if self.control_event_id is not None:
            _strict_required_text(self.control_event_id, "control_event_id")
        for name in ("initialization_generation", "alignment_revision"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or null.")
        if self.measurement_ready is not None and type(self.measurement_ready) is not bool:
            raise ValueError("measurement_ready must be a boolean or null.")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "status": self.status.value,
            "occurred_at": self.occurred_at,
            "frame_seq": self.frame_seq,
            "image_name": self.image_name,
            "error": self.error,
        }
        if self.enabled is not None:
            payload["enabled"] = self.enabled
        if self.control_revision is not None:
            payload["control_revision"] = self.control_revision
        if self.experiment_started_at is not None:
            payload["experiment_started_at"] = self.experiment_started_at
        if self.control_event_id is not None:
            payload["control_event_id"] = self.control_event_id
        for name in ("initialization_generation", "alignment_revision", "measurement_ready"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "GrowthRateStatusPayload":
        return cls(
            run_id=_mapping_text(payload, "run_id"),
            status=GrowthRateStatus(_mapping_text(payload, "status")),
            occurred_at=_mapping_text(payload, "occurred_at"),
            frame_seq=_optional_int(payload.get("frame_seq")),
            image_name=_optional_text(payload.get("image_name")),
            error=_optional_text(payload.get("error")),
            enabled=_mapping_optional_bool(payload, "enabled"),
            control_revision=_mapping_optional_nonnegative_int(
                payload,
                "control_revision",
            ),
            experiment_started_at=_mapping_optional_strict_text(
                payload,
                "experiment_started_at",
            ),
            control_event_id=_mapping_optional_strict_text(
                payload,
                "control_event_id",
            ),
            initialization_generation=_mapping_optional_nonnegative_int(payload, "initialization_generation"),
            alignment_revision=_mapping_optional_nonnegative_int(payload, "alignment_revision"),
            measurement_ready=_mapping_optional_bool(payload, "measurement_ready"),
        )


def _mapping_text(payload: Mapping[str, Any], key: str) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("Message payload must be an object.")
    return _required_text(payload.get(key), key)


def _mapping_int(payload: Mapping[str, Any], key: str) -> int:
    if key not in payload:
        raise ValueError(f"{key} is required.")
    try:
        return int(payload[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer.") from exc


def _mapping_strict_text(payload: Mapping[str, Any], key: str) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("Message payload must be an object.")
    if key not in payload:
        raise ValueError(f"{key} is required.")
    return _strict_required_text(payload[key], key)


def _mapping_strict_positive_int(payload: Mapping[str, Any], key: str) -> int:
    if not isinstance(payload, Mapping):
        raise ValueError("Message payload must be an object.")
    if key not in payload:
        raise ValueError(f"{key} is required.")
    value = payload[key]
    if type(value) is not int or value < 1:
        raise ValueError(f"{key} must be a positive integer.")
    return value


def _mapping_optional_nonnegative_int(
    payload: Mapping[str, Any],
    key: str,
) -> int | None:
    if not isinstance(payload, Mapping):
        raise ValueError("Message payload must be an object.")
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{key} must be a nonnegative integer or null.")
    return value


def _mapping_float(payload: Mapping[str, Any], key: str) -> float:
    if key not in payload:
        raise ValueError(f"{key} is required.")
    try:
        return float(payload[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number.") from exc


def _mapping_bool(payload: Mapping[str, Any], key: str) -> bool:
    if key not in payload:
        raise ValueError(f"{key} is required.")
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean.")
    return value


def _mapping_optional_bool(payload: Mapping[str, Any], key: str) -> bool | None:
    if not isinstance(payload, Mapping):
        raise ValueError("Message payload must be an object.")
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean or null.")
    return value


def _mapping_optional_strict_text(
    payload: Mapping[str, Any],
    key: str,
) -> str | None:
    if not isinstance(payload, Mapping):
        raise ValueError("Message payload must be an object.")
    value = payload.get(key)
    if value is None:
        return None
    return _strict_required_text(value, key)


def _required_text(value: Any, key: str) -> str:
    if value is None:
        raise ValueError(f"{key} is required.")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{key} is required.")
    return text


def _strict_required_text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be nonempty text.")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


__all__ = [
    "CONTROLLER_ADAPTATION_MODES",
    "GROWTH_RATE_UNIT",
    "ControllerAddSeedPayload",
    "ControllerAdaptationPayload",
    "ExperimentStartPayload",
    "ExperimentStopPayload",
    "GsensorActivationPayload",
    "GrowthRateSamplePayload",
    "GrowthRateStatus",
    "GrowthRateStatusPayload",
]
