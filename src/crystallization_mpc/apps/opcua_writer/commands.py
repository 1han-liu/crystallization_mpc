"""Validation of the narrowly scoped process.setpoint.write contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from crystallization_mpc.apps.controller.process import _finite_number
from crystallization_mpc.apps.controller.process import ProcessSafetyError
from crystallization_mpc.messaging.schema import build_envelope

COMMAND_ROUTE = "controller.to.opcua_writer"
RESULT_ROUTE = "opcua_writer.to.process"


def timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO-8601 timestamp with timezone.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp with timezone.") from exc
    if result.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone.")
    return result.astimezone(timezone.utc)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{field} must be a nonempty string of at most 256 characters.")
    return value


@dataclass(frozen=True)
class WriteCommand:
    command_id: str
    run_id: str
    run_started_at: datetime
    tick_seq: int
    setpoint_K: float
    expires_at: datetime
    run_started_at_text: str | None = None

    @classmethod
    def from_envelope(cls, message: Mapping[str, Any]) -> "WriteCommand":
        if (
            message.get("ver") != 1
            or message.get("name") != "process.setpoint.write"
            or message.get("src") != "controller"
            or message.get("dst") != "opcua_writer"
            or message.get("msg_type") != "command"
        ):
            raise ValueError("Invalid process.setpoint.write envelope.")
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("Command payload must be an object.")
        command_id = _identifier(payload.get("command_id"), "command_id")
        if message.get("correlation_id") != command_id:
            raise ValueError("correlation_id must equal command_id.")
        tick = payload.get("tick_seq")
        if isinstance(tick, bool) or not isinstance(tick, int) or not 0 <= tick <= 2**63 - 1:
            raise ValueError("tick_seq must be a non-negative signed 64-bit integer.")
        started = timestamp(payload.get("run_started_at"), "run_started_at")
        expires = timestamp(payload.get("expires_at"), "expires_at")
        if expires <= started:
            raise ValueError("expires_at must be after run_started_at.")
        try:
            setpoint = _finite_number(payload.get("setpoint_K"), "setpoint_K")
        except OverflowError as exc:
            raise ProcessSafetyError("setpoint_K must be finite.") from exc
        return cls(
            command_id=command_id,
            run_id=_identifier(payload.get("run_id"), "run_id"),
            run_started_at=started,
            tick_seq=tick,
            setpoint_K=setpoint,
            expires_at=expires,
            run_started_at_text=payload["run_started_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "run_id": self.run_id,
            "run_started_at": self.run_started_at_text or self.run_started_at.isoformat().replace("+00:00", "Z"),
            "tick_seq": self.tick_seq,
            "setpoint_K": self.setpoint_K,
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
        }

    def fingerprint(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    def result(self, status: str, error: str | None = None, *, write_result=None) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "run_id": self.run_id,
            "run_started_at": self.to_dict()["run_started_at"],
            "tick_seq": self.tick_seq,
            "status": status,
            "error": error,
            "write_result": write_result,
        }


def result_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return build_envelope(
        src="opcua_writer",
        dst="process",
        msg_type="event",
        name="process.setpoint.result",
        seq=payload.get("tick_seq", 0),
        payload=payload,
        correlation_id=payload.get("command_id"),
    )
