"""Actual paper-stage boundary annotations for the existing Grafana dashboard.

This module records test orchestration evidence, never controller commands or
synthetic measurements. It uses the existing annotation measurement with an
explicit test-producer tag and verifies the persisted fields independently.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from crystallization_mpc.infra.influxdb.client import InfluxSettings
from crystallization_mpc.infra.influxdb.write import InfluxWriter
from .acceptance import PHASE_NAMES

MEASUREMENT = "controller_runtime_event"
PRODUCER = "prosys-acceptance"
SOURCE = "Synthetic G / model-generated external process"


def _utc(value: str | datetime) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("A phase boundary needs an actual timezone-aware timestamp.")
    return stamp.astimezone(timezone.utc)


class PhaseEvents:
    def __init__(self, writer: Any, *, query_rows: Callable[[str], list[Mapping]], bucket: str):
        self.writer, self.query_rows, self.bucket = writer, query_rows, bucket
        self.events: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: Path) -> "PhaseEvents":
        # Never load production .env or silently fall back to production ports.
        settings = json.loads(path.read_text(encoding="utf-8"))
        if set(settings) != {"url", "org", "bucket", "token"} or not all(
            isinstance(value, str) and value for value in settings.values()
        ):
            raise ValueError("Explicit test telemetry URL, org, bucket and token are required.")
        parsed = urlparse(settings["url"])
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.port != 8088):
            raise ValueError("Phase annotations may only target the isolated localhost:8088 Influx instance.")
        writer = InfluxWriter(InfluxSettings(**settings))
        return cls(writer, bucket=settings["bucket"], query_rows=lambda query: [
            dict(record.values) for table in writer.client.query_api().query(query)
            for record in table.records
        ])

    def record(self, *, phase: int, run_id: str, started_at: str, tick: int,
               elapsed_s: float, applied_at: str) -> dict[str, Any]:
        """Call only after an actual committed output/configuration boundary."""
        if type(phase) is not int or phase not in PHASE_NAMES or type(tick) is not int or tick < 1:
            raise ValueError("A valid paper phase and positive committed tick are required.")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("An experiment identity is required.")
        if isinstance(elapsed_s, bool) or not isinstance(elapsed_s, (float, int)) or not math.isfinite(elapsed_s) or elapsed_s < 0:
            raise ValueError("Elapsed time must be finite and nonnegative.")
        start, applied = _utc(started_at), _utc(applied_at)
        if applied < start:
            raise ValueError("A phase cannot precede the experiment start.")
        event = {"phase": phase, "run_id": run_id, "run_started_at": start.isoformat(),
                 "effective_tick": tick, "elapsed_s": float(elapsed_s),
                 "applied_at": applied.isoformat(), "event_id": f"prosys:phase:{phase}:{tick}"}
        if self.events:
            previous = self.events[-1]
            if event == previous:
                return deepcopy(previous)  # one local event, no duplicate write
            if previous["run_id"] != run_id or previous["run_started_at"] != event["run_started_at"]:
                raise ValueError("A phase recorder cannot mix experiment identities.")
            if (phase != previous["phase"] + 1 or tick < previous["effective_tick"]
                    or elapsed_s < previous["elapsed_s"] or applied < _utc(previous["applied_at"])):
                raise ValueError("Record each actual phase boundary once in chronological order.")
        elif phase != 1:
            raise ValueError("The first recorded paper phase must be onset preparation.")
        title = f"Paper phase {phase}: {PHASE_NAMES[phase]}"
        fields = {key: value for key, value in event.items() if key not in {"run_id", "event_id"}}
        fields.update(title=title, text=f"{title}; effective tick {tick}; {SOURCE}",
                      event_name="paper_phase_started", source=SOURCE)
        self.writer.write_tagged_fields(fields, tags={"run_id": run_id, "event_id": event["event_id"],
            "producer": PRODUCER}, measurement=MEASUREMENT, timestamp=applied)
        # A write exception must not advance the local acknowledged-event list.
        self.events.append(event)
        return deepcopy(event)

    def verify(self) -> dict[str, Any]:
        if not self.events:
            return {"status": "NOT_RUN", "expected_events": 0, "verified_events": 0}
        first, last = self.events[0], self.events[-1]
        lower = _utc(first["applied_at"]).isoformat()
        upper = (_utc(last["applied_at"]) + timedelta(seconds=1)).isoformat()
        query = (f"from(bucket: {json.dumps(self.bucket)})\n"
                 f"  |> range(start: time(v: {json.dumps(lower)}), stop: time(v: {json.dumps(upper)}))\n"
                 f"  |> filter(fn: (r) => r._measurement == {json.dumps(MEASUREMENT)} and r.run_id == {json.dumps(first['run_id'])})\n"
                 f"  |> filter(fn: (r) => exists r.producer and r.producer == {json.dumps(PRODUCER)})")
        return self.verify_rows(self.query_rows(query))

    def verify_rows(self, rows: list[Mapping]) -> dict[str, Any]:
        expected = {event["event_id"]: event for event in self.events}
        grouped: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for row in rows:
            identity = row.get("event_id")
            if identity not in expected:
                errors.append("Unexpected test phase event in the selected experiment interval.")
                continue
            event = expected[identity]
            try:
                valid_identity = (
                    row.get("_measurement") == MEASUREMENT
                    and row.get("run_id") == event["run_id"]
                    and row.get("producer") == PRODUCER
                    and _utc(row["_time"]) == _utc(event["applied_at"])
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                valid_identity = False
            if not valid_identity:
                errors.append(f"Annotation identity/time mismatch: {identity}")
                continue
            fields = grouped.setdefault(identity, {})
            key = row.get("_field")
            if not isinstance(key, str) or "_value" not in row:
                errors.append(f"Malformed persisted annotation field: {identity}")
                continue
            if key in fields:
                errors.append(f"Duplicate persisted annotation field: {identity}/{key}")
            fields[key] = row["_value"]
        verified = 0
        for identity, event in expected.items():
            fields = grouped.get(identity, {})
            checks = {key: event[key] for key in ("phase", "run_started_at", "effective_tick", "elapsed_s", "applied_at")}
            checks.update(source=SOURCE, event_name="paper_phase_started",
                          title=f"Paper phase {event['phase']}: {PHASE_NAMES[event['phase']]}")
            text = f"{checks['title']}; effective tick {event['effective_tick']}; {SOURCE}"
            checks["text"] = text
            if all(fields.get(key) == value for key, value in checks.items()):
                verified += 1
            else:
                errors.append(f"Persisted annotation fields missing or different: {identity}")
        status = "FAIL" if errors else "PASS" if verified == 4 else "INCOMPLETE" if expected else "NOT_RUN"
        return {"status": status, "expected_events": len(expected), "verified_events": verified,
                "errors": errors, "events": deepcopy(self.events)}

    def close(self) -> None:
        self.writer.close()


__all__ = ["PhaseEvents"]
