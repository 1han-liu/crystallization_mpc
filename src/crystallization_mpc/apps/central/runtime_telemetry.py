"""Retryable configuration-event export, separate from process measurements."""
from __future__ import annotations

import json
import os
from datetime import datetime

from crystallization_mpc.infra.influxdb.client import InfluxSettings
from crystallization_mpc.infra.influxdb.durable import DurableInfluxWriter, telemetry_path

RUNTIME_EVENT_MEASUREMENT = "controller_runtime_event"


def create_runtime_writer(root=None):
    return DurableInfluxWriter(load_runtime_influx_settings(), path=telemetry_path("central", root))


def load_runtime_influx_settings() -> InfluxSettings:
    # Match Controller's destination and credential precedence, using only the
    # process environment supplied by Compose or the native service launcher.
    values = {
        key.lower(): (os.getenv(f"CONTROLLER_INFLUX_{key}", "").strip()
                      or os.getenv(f"INFLUX_{key}", default).strip())
        for key, default in {
            "URL": "http://influxdb:8086", "TOKEN": "",
            "ORG": "lab", "BUCKET": "process",
        }.items()
    }
    if not values["token"]:
        raise RuntimeError("INFLUX_TOKEN or CONTROLLER_INFLUX_TOKEN is required.")
    return InfluxSettings(**values)


def write_runtime_event(writer, entry):
    result = entry["result"]
    if result.get("status") != "applied" or result.get("no_change"):
        return False
    # Old events lack an exact apply timestamp. Never substitute request time.
    timestamp = result.get("applied_at")
    if not timestamp:
        raise ValueError("Exact Controller application time unavailable")
    time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if time.tzinfo is None:
        raise ValueError("Application time must include a timezone")
    parameter_changes = result.get("parameter_changes") or (
        entry["command"].get("changes") if entry["command"].get("experiment_started_at") else None)
    if parameter_changes is not None:
        before = result.get("parameter_before")
        after = result.get("parameter_values")
        summary = "; ".join(f"{section}.{key} = {value}"
                            for section, changes in parameter_changes.items()
                            for key, value in changes.items())
    else:
        before, after = result["before"], result["configuration"]
        summary = "; ".join(f"{key}: {before.get(key)} → {after.get(key)}"
                            for key in entry["command"]["changes"] if before.get(key) != after.get(key))
    fields = {"title": "Run parameters applied" if parameter_changes is not None else "Runtime configuration applied",
              "text": f"{summary}; revision {result['revision']}; effective tick {result['effective_tick']}",
              "before": json.dumps(before, sort_keys=True), "after": json.dumps(after, sort_keys=True),
              "revision": result["revision"], "effective_tick": result["effective_tick"],
              "requested_at": result["requested_at"], "applied_at": timestamp}
    if parameter_changes is not None:
        fields["parameter_changes"] = json.dumps(parameter_changes, sort_keys=True)
        fields.update(event_type="experiment.parameters.update", status="applied", is_effective=True)
        frame = result.get("effective_after_frame_seq", result.get("gsensor_effective_after_frame_seq"))
        if frame is not None:
            fields["gsensor_effective_after_frame_seq"] = frame
            fields["effective_after_frame_seq"] = frame
        for key in ("measurement_revision", "controller_applied"):
            if key in result:
                fields[key] = result[key]
    # Same tags + exact time overwrite the same point on retry, including after a crash.
    record_id = writer.write_tagged_fields(fields, tags={"run_id": result["run_id"], "event_id": result["event_id"]},
                              measurement=RUNTIME_EVENT_MEASUREMENT, timestamp=time)
    return writer.is_delivered(record_id) if isinstance(writer, DurableInfluxWriter) else True


def export_pending(history, writer):
    for entry in history.pending_exports():
        if not entry["result"].get("applied_at"):
            history.save({**entry, "grafana_sync": "time_unavailable", "grafana_error": "Exact application time unavailable in legacy record"})
            continue
        try:
            delivered = write_runtime_event(writer, entry)
        except Exception as exc:
            history.mark_export(entry, error=("Application time unavailable" if not entry["result"].get("applied_at")
                                              else type(exc).__name__))
            # Retry next pass; do not leak tokens/URLs from client exceptions.
            break
        else:
            if delivered:
                history.mark_export(entry)
