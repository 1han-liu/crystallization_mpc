"""Retryable configuration-event export, separate from process measurements."""
from __future__ import annotations

import json
import os
from datetime import datetime

from crystallization_mpc.infra.influxdb.client import InfluxSettings

RUNTIME_EVENT_MEASUREMENT = "controller_runtime_event"


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
    before, after = result["before"], result["configuration"]
    summary = "; ".join(f"{key}: {before.get(key)} → {after.get(key)}"
                        for key in entry["command"]["changes"] if before.get(key) != after.get(key))
    fields = {"title": "Runtime configuration applied",
              "text": f"{summary}; revision {result['revision']}; effective tick {result['effective_tick']}",
              "before": json.dumps(before, sort_keys=True), "after": json.dumps(after, sort_keys=True),
              "revision": result["revision"], "effective_tick": result["effective_tick"],
              "requested_at": result["requested_at"], "applied_at": timestamp}
    # Same tags + exact time overwrite the same point on retry, including after a crash.
    writer.write_tagged_fields(fields, tags={"run_id": result["run_id"], "event_id": result["event_id"]},
                              measurement=RUNTIME_EVENT_MEASUREMENT, timestamp=time)
    return True


def export_pending(history, writer):
    for entry in history.pending_exports():
        if not entry["result"].get("applied_at"):
            history.save({**entry, "grafana_sync": "time_unavailable", "grafana_error": "Exact application time unavailable in legacy record"})
            continue
        try:
            write_runtime_event(writer, entry)
        except Exception as exc:
            history.mark_export(entry, error=("Application time unavailable" if not entry["result"].get("applied_at")
                                              else type(exc).__name__))
            # Retry next pass; do not leak tokens/URLs from client exceptions.
            break
        else:
            history.mark_export(entry)
