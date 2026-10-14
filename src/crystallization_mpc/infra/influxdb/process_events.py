"""Common durable audit format for the command and acknowledgement path."""
from __future__ import annotations

import json
import hashlib
import time
from typing import Any

PROCESS_EVENT_MEASUREMENT = "process_io_event"


def process_event(payload: dict[str, Any], service: str, stage: str, *, timestamp: int | None = None):
    command_id = str(payload.get("command_id") or "invalid-" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest())
    event_id = f"{command_id}:{service}:{stage}"
    fields = {"stage": stage, "tick_seq": int(payload.get("tick_seq", 0)),
              "payload": json.dumps(payload, sort_keys=True, allow_nan=False),
              "status": str(payload.get("status") or stage)}
    for key in ("setpoint_K", "run_started_at", "expires_at", "error"):
        if payload.get(key) is not None:
            fields[key] = payload[key]
    for key, value in (payload.get("write_result") or {}).items():
        if value is not None:
            fields[f"write_{key}"] = value
    return {"fields": fields, "tags": {"service": service, "run_id": str(payload.get("run_id") or ""),
                                        "command_id": command_id, "event_id": event_id},
            "measurement": PROCESS_EVENT_MEASUREMENT, "timestamp": timestamp if timestamp is not None else time.time_ns(),
            "event_id": event_id}


def write_process_event(writer, payload, service, stage, *, timestamp=None):
    record = process_event(payload, service, stage, timestamp=timestamp)
    # Third-party injected synchronous writers keep the existing narrow API.
    if not getattr(writer, "asynchronous", False):
        record.pop("event_id")
    return writer.write_tagged_fields(**record)
