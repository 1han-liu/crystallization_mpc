"""Repeatable per-experiment CSV exports, with explicit completeness metadata.

Influx timestamps are converted to decimal strings *inside Flux*, before the
Python client can truncate them to datetime's microsecond resolution. Raw OPC UA
rounds are joined only on (device, exact collection timestamp), never on arrival.
The caller owns the stop/flush barrier: ``ready=False`` must be used while any
producer or durable outbox can still add records for the experiment.
"""
from __future__ import annotations

import calendar
import csv
import json
import math
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import yaml

from crystallization_mpc.infra.influxdb.client import InfluxSettings, create_influx_client

CSV_FILENAMES = (
    "parameters.csv", "process_data.csv", "controller_data.csv",
    "gsensor_data.csv", "events.csv",
)
ARCHIVE_FILENAME = "archive.json"
CAPTURE_FILENAME = "capture.json"
PROCESS_FIELDS = ("T", "T_j", "c", "count_middle", "T_j_set")
GOOD_QUALITY = frozenset({"OK (0x0)", "The operation succeeded. StatusGood (0x0)"})
BASE_COLUMNS = ("run_id", "time_ns", "time_utc")
PARAMETER_COLUMNS = (
    "run_id", "source", "scope", "parameter", "value", "effective_at",
    "event_id", "effective_tick", "revision", "parameter_version", "status", "is_effective", "requested_at",
)
PROCESS_COLUMNS = (
    *BASE_COLUMNS, "device", *PROCESS_FIELDS, "temperature_unit",
    *(f"{name}_node_id" for name in PROCESS_FIELDS),
    *(f"{name}_quality" for name in PROCESS_FIELDS),
    "missing_fields", "invalid_fields", "complete", "node_records_json",
)
CONTROLLER_COLUMNS = (*BASE_COLUMNS, "tick_seq", "T", "T_j", "c", "T_j_set")
GSENSOR_COLUMNS = (*BASE_COLUMNS, "frame_seq", "image_name", "valid", "G_u", "G_v", "unit")
EVENT_COLUMNS = (
    *BASE_COLUMNS, "measurement", "event_id", "event_type", "service", "command_id",
    "stage", "status", "tick_seq", "setpoint_K", "write_setpoint_K", "write_setpoint_C",
    "write_readback_K", "write_verified", "error", "payload",
)
_RFC3339 = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)


class ExportSource(Protocol):
    def fetch(
        self, measurement: str, *, run_id: str | None = None,
        device: str | None = None, start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> Iterable[Mapping[str, Any]]: ...


def timestamp_ns(value: Any) -> int:
    """Parse integer nanoseconds or timezone-qualified RFC3339 without floats."""
    if type(value) is int:
        return value
    if not isinstance(value, str):
        raise ValueError("Timestamp must be integer nanoseconds or RFC3339 text.")
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    match = _RFC3339.fullmatch(value)
    if not match:
        raise ValueError("Timestamp must contain a timezone and at most nine fractional digits.")
    whole, fraction, offset = match.groups()
    parsed = datetime.fromisoformat(whole + ("+00:00" if offset == "Z" else offset))
    seconds = calendar.timegm(parsed.astimezone(timezone.utc).timetuple())
    return seconds * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def timestamp_text(value: int) -> str:
    seconds, nanos = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def _flux_string(value: str) -> str:
    # JSON string escaping is compatible with Flux string literals.
    return json.dumps(str(value), ensure_ascii=False)


class InfluxExportSource:
    """Read wide records while retaining nanoseconds, without a pandas dependency."""

    def __init__(self, settings: InfluxSettings, *, client_factory=create_influx_client):
        self.settings = settings
        self._client_factory = client_factory

    @classmethod
    def from_environment(cls) -> "InfluxExportSource":
        values = {
            name.lower(): (os.getenv(f"CONTROLLER_INFLUX_{name}", "").strip()
                           or os.getenv(f"INFLUX_{name}", default).strip())
            for name, default in {"URL": "http://influxdb:8086", "TOKEN": "",
                                  "ORG": "lab", "BUCKET": "process"}.items()
        }
        if not values["token"]:
            raise RuntimeError("INFLUX_TOKEN or CONTROLLER_INFLUX_TOKEN is required for CSV export.")
        return cls(InfluxSettings(**values))

    def fetch(self, measurement, *, run_id=None, device=None, start_ns=None, end_ns=None):
        start = f'time(v: {_flux_string(timestamp_text(start_ns))})' if start_ns is not None else 'time(v: 0)'
        stop = f', stop: time(v: {_flux_string(timestamp_text(end_ns))})' if end_ns is not None else ''
        conditions = [f'r._measurement == {_flux_string(measurement)}']
        if run_id is not None:
            conditions.append(f'exists r.run_id and r.run_id == {_flux_string(run_id)}')
        if device is not None:
            conditions.append(f'exists r.device and r.device == {_flux_string(device)}')
        query = (
            f'from(bucket: {_flux_string(self.settings.bucket)})\n'
            f'  |> range(start: {start}{stop})\n'
            f'  |> filter(fn: (r) => {" and ".join(conditions)})\n'
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            '  |> map(fn: (r) => ({r with time_ns: string(v: int(v: r._time)), time_utc: string(v: r._time)}))\n'
            '  |> drop(columns: ["_time", "_start", "_stop"])'
        )
        client = self._client_factory(self.settings)
        try:
            for record in client.query_api().query_stream(query=query, org=self.settings.org):
                yield dict(record.values)
        finally:
            client.close()


class ExperimentExporter:
    def __init__(self, registry, source: ExportSource | None = None):
        self.registry = registry
        self.source = source
        self._lock = threading.RLock()

    def read_status(self, run_id: str) -> dict[str, Any]:
        path = self.registry.run_dir(run_id) / ARCHIVE_FILENAME
        if not path.exists():
            return {"schema_version": 1, "run_id": run_id, "status": "pending", "rows": {},
                    "coverage": {}, "errors": [], "pending_reasons": ["Export has not been requested."]}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("run_id") != run_id:
            raise ValueError("Invalid experiment archive status.")
        return data

    def export(
        self, run_id: str, *, ready: bool = True,
        pending_reasons: Iterable[str] = (), capture: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Export one stopped, flushed run; callers may safely retry after restart.

        Re-running always queries storage again and replaces complete files. A
        crash leaves ``exporting`` in archive.json, which the host retries rather
        than presenting partly replaced files as a completed archive.
        """
        with self._lock:
            directory = self.registry.run_dir(run_id)
            previous = self.read_status(run_id)
            state = {"schema_version": 1, "run_id": run_id, "status": "pending",
                     "updated_at": timestamp_text(time.time_ns()), "rows": {}, "coverage": {},
                     "errors": [], "pending_reasons": list(pending_reasons),
                     "attempt": int(previous.get("attempt", 0)) + int(ready),
                     "files": list(CSV_FILENAMES)}
            if not ready:
                if not state["pending_reasons"]:
                    state["pending_reasons"] = ["Waiting for producers and pending database writes."]
                _atomic_json(directory / ARCHIVE_FILENAME, state)
                return state
            state["status"] = "exporting"
            _atomic_json(directory / ARCHIVE_FILENAME, state)
            try:
                manifest = self.registry.get(run_id).to_dict()
                if not manifest.get("ended_at") or not manifest.get("started_at"):
                    raise ValueError("Experiment must have a recorded start and end before export.")
                active_capture = dict(capture) if capture is not None else _read_optional_json(directory / CAPTURE_FILENAME)
                if active_capture and active_capture.get("run_id", run_id) != run_id:
                    raise ValueError("Capture metadata belongs to a different experiment.")
                source = self.source if self.source is not None else InfluxExportSource.from_environment()
                data = self._collect(run_id, directory, manifest, active_capture, source, state)
                for filename, (columns, rows) in data.items():
                    _atomic_csv(directory / filename, columns, rows)
                    state["rows"][filename] = len(rows)
                state["status"] = "partial" if state["errors"] else "completed"
                state["pending_reasons"] = []
                state["completed_at"] = timestamp_text(time.time_ns())
            except Exception as exc:
                state["status"] = "failed"
                # Upstream client exceptions may contain URLs or credentials.
                message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else type(exc).__name__
                state["errors"].append({"code": "export_failed", "message": message})
            state["updated_at"] = timestamp_text(time.time_ns())
            _atomic_json(directory / ARCHIVE_FILENAME, state)
            return state

    def _collect(self, run_id, directory, manifest, capture, source, state):
        def problem(code, message):
            state["errors"].append({"code": code, "message": message})

        controller = _records(source.fetch("controller_measurement", run_id=run_id), run_id)
        gsensor = _records(source.fetch("gsensor_measurement", run_id=run_id), run_id)
        runtime_events = _records(source.fetch("controller_runtime_event", run_id=run_id), run_id)
        io_events = _records(source.fetch("process_io_event", run_id=run_id), run_id)
        runtime_events, history_coverage = _runtime_history_events(
            run_id, Path(getattr(self.registry, "root", directory.parent)), runtime_events,
        )
        state["coverage"]["runtime_events"] = history_coverage
        if history_coverage["status"] == "partial":
            problem("runtime_history_incomplete", history_coverage["reason"])
        command_coverage = _command_coverage(controller, io_events)
        state["coverage"]["process_events"] = command_coverage
        if command_coverage["status"] == "partial":
            problem("command_history_incomplete", command_coverage["reason"])
        gsensor_enabled = capture.get("gsensor_enabled") if capture else None
        for component, records, sequence_key, marker_key in (
            ("controller", controller, "tick_seq", "last_tick"),
            ("gsensor", gsensor, "frame_seq", "last_frame"),
        ):
            marker = _read_optional_json(directory / f"{component}_finalized.json")
            if marker and marker.get("run_id") != run_id:
                raise ValueError(f"{component} finalization marker belongs to a different experiment.")
            enabled = True if component == "controller" else gsensor_enabled
            coverage = _sequence_coverage(records, sequence_key, marker, marker_key, enabled)
            state["coverage"][component] = coverage
            if coverage["status"] == "partial" or (enabled is True and coverage["status"] == "unavailable"):
                problem(f"{component}_data_missing", coverage["reason"])
        process, raw_coverage = _process_data(run_id, manifest, capture, source)
        state["coverage"]["process"] = raw_coverage
        if raw_coverage["status"] not in {"available", "not_applicable"}:
            problem("process_data_incomplete", raw_coverage["reason"])
        events = sorted([*runtime_events, *io_events, *_lifecycle_events(run_id, manifest)],
                        key=lambda row: (int(row["time_ns"]) if row.get("time_ns") is not None else 2**63,
                                         str(row.get("event_id", ""))))
        snapshot_name = manifest.get("params_snapshot_file") or "params_snapshot.yaml"
        snapshot_path = (directory / snapshot_name).resolve()
        if snapshot_path.parent != directory.resolve():
            raise ValueError("Parameter snapshot path must be inside the experiment directory.")
        if snapshot_path.exists():
            snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
            if not isinstance(snapshot, dict):
                raise ValueError("Experiment parameter snapshot must be a mapping.")
        else:
            snapshot = {}
            problem("parameter_snapshot_missing", "The experiment parameter snapshot is unavailable.")
        parameters = _parameter_rows(run_id, snapshot, capture, runtime_events, manifest)
        return {
            "parameters.csv": (PARAMETER_COLUMNS, parameters),
            "process_data.csv": (PROCESS_COLUMNS, process),
            "controller_data.csv": (CONTROLLER_COLUMNS, controller),
            "gsensor_data.csv": (GSENSOR_COLUMNS, gsensor),
            "events.csv": (EVENT_COLUMNS, events),
        }


def _records(records, run_id):
    result = []
    seen = set()
    for record in records:
        if record.get("run_id") != run_id:
            continue  # Defensive isolation, also applied to injected sources.
        row = {key: value for key, value in record.items()
               if key not in {"result", "table", "_start", "_stop", "_time", "_time_ns", "_measurement"}}
        stamp = _record_time(record)
        row.update(run_id=run_id, time_ns=stamp, time_utc=timestamp_text(stamp))
        if record.get("_measurement"):
            row["measurement"] = record["_measurement"]
        if row.get("measurement") == "process_io_event" and not row.get("event_type"):
            # Keep the producer's actual stage as a separate stable CSV column.
            row["event_type"] = "process.setpoint.write" if row.get("stage") in {
                "prepared", "published", "executing",
            } else "process.setpoint.result"
        signature = json.dumps(_json_safe(row), sort_keys=True, ensure_ascii=False)
        if signature not in seen:
            seen.add(signature)
            result.append(row)
    return sorted(result, key=lambda row: (row["time_ns"], str(row.get("event_id", ""))))


def _record_time(record):
    for key in ("time_ns", "_time_ns", "time_utc", "_time"):
        if key in record and record[key] is not None:
            return timestamp_ns(record[key])
    raise ValueError("Stored record lacks a precise timestamp.")


def _sequence_coverage(records, sequence_key, marker, marker_key, enabled):
    """Check producer watermarks as well as rows, including runs with zero ticks."""
    expected = marker.get(marker_key) if marker else None
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ValueError("Producer finalization sequence must be a nonnegative integer.")
    sequence = set()
    invalid_rows = 0
    for record in records:
        value = record.get(sequence_key)
        if type(value) is int and value >= 1:
            sequence.add(value)
        else:
            invalid_rows += 1
    coverage = {
        "enabled": enabled, "rows": len(records), "expected_last_sequence": expected,
        "unique_sequences": len(sequence), "last_sequence": max(sequence) if sequence else None,
        "invalid_sequence_rows": invalid_rows,
        "persistence_enabled": marker.get("persistence_enabled") if marker else None,
        "enqueue_failures": marker.get("enqueue_failures", 0) if marker else 0,
    }
    if expected is not None:
        present = sorted(value for value in sequence if value <= expected)
        missing_ranges = []
        previous = 0
        for value in present:
            if value > previous + 1:
                missing_ranges.append([previous + 1, value - 1])
            previous = value
        if previous < expected:
            missing_ranges.append([previous + 1, expected])
        unexpected = sorted(value for value in sequence if value > expected)
        coverage.update(missing_sequence_ranges=missing_ranges, unexpected_sequences=unexpected)
        if missing_ranges or unexpected or invalid_rows or len(records) != len(sequence) or coverage["enqueue_failures"]:
            coverage.update(status="partial", reason="Stored measurement sequences do not match the producer finalization watermark.")
        else:
            coverage.update(status="available" if records or enabled is True else "not_applicable",
                            reason="Stored measurement sequences match the producer finalization watermark.")
    elif records:
        coverage.update(status="available" if not invalid_rows else "partial",
                        reason="Stored measurements are available; no producer watermark exists for count verification.")
    elif enabled is False:
        coverage.update(status="not_applicable", reason="This measurement component was disabled.")
    else:
        coverage.update(status="unavailable", reason="No stored measurements or zero-measurement finalization proof were found.")
    return coverage


def _command_coverage(controller, events):
    """Separate complete audit records from successful device outcomes."""
    expected = {row["tick_seq"] for row in controller if row.get("process_write_attempted") is True
                and type(row.get("tick_seq")) is int}
    by_command: dict[str, list[dict]] = {}
    for row in events:
        if row.get("command_id"):
            by_command.setdefault(str(row["command_id"]), []).append(row)
    prepared_ticks = set()
    unconfirmed, missing_writer = [], []
    for command_id, records in by_command.items():
        controller_records = [row for row in records if row.get("service") == "controller"]
        prepared = [row for row in controller_records if row.get("stage") == "prepared"]
        prepared_ticks.update(row["tick_seq"] for row in prepared if type(row.get("tick_seq")) is int)
        receipts = [row for row in controller_records if row.get("stage") == "receipt"]
        published = [row for row in controller_records if row.get("stage") == "published"]
        terminal = receipts or [row for row in controller_records if row.get("stage") == "unknown"]
        if prepared and not terminal:
            unconfirmed.append(command_id)
        if (receipts or published) and not any(
            row.get("service") == "opcua_writer" and row.get("stage") in {"result", "invalid"}
            for row in records
        ):
            missing_writer.append(command_id)
    missing = sorted(expected - prepared_ticks)
    partial = bool(missing or unconfirmed or missing_writer)
    return {"status": "partial" if partial else ("available" if events else "not_applicable"),
            "rows": len(events), "expected_write_ticks": sorted(expected), "missing_prepared_ticks": missing,
            "missing_controller_outcome_commands": sorted(unconfirmed), "missing_writer_result_commands": sorted(missing_writer),
            "reason": ("Some attempted writes lack their persisted command or outcome audit records."
                       if partial else "Available command audit records include the expected outcomes; outcome unknown is preserved as unknown.")}


def _process_data(run_id, manifest, capture, source):
    coverage: dict[str, Any] = {"status": "unavailable", "rows": 0, "complete_rounds": 0,
                                "incomplete_rounds": 0, "gap_count": 0}
    if not capture:
        coverage["reason"] = "Legacy experiment has no raw-data capture metadata; historical raw coverage cannot be verified."
        return [], coverage
    if capture.get("process_enabled") is False:
        coverage.update(status="not_applicable", reason="Process communication was disabled for this experiment.")
        return [], coverage
    device = capture.get("device")
    if not isinstance(device, str) or not device.strip():
        coverage["reason"] = "Experiment capture metadata does not identify the OPC UA device."
        return [], coverage
    start = timestamp_ns(capture.get("start_ns") if capture.get("start_ns") is not None else manifest["started_at"])
    end = timestamp_ns(capture.get("end_ns") if capture.get("end_ns") is not None else manifest["ended_at"])
    if end <= start:
        raise ValueError("Raw-data capture end must be after its start.")
    interval = Decimal(str(capture.get("telemetry_interval_s", 1)))
    if not interval.is_finite() or interval <= 0:
        raise ValueError("Telemetry interval must be a positive finite number.")
    interval_ns = int(interval * 1_000_000_000)
    if interval_ns < 1:
        raise ValueError("Telemetry interval must be at least one nanosecond.")
    groups: dict[int, list[dict]] = {}
    for record in source.fetch("crystallizer_raw", device=device, start_ns=start, end_ns=end):
        if record.get("device") != device:
            continue
        stamp = _record_time(record)
        if start <= stamp < end:
            groups.setdefault(stamp, []).append(dict(record))
    rows = []
    node_ids = capture.get("node_ids") or {}
    if not isinstance(node_ids, dict):
        raise ValueError("Capture node_ids must map field names to OPC UA NodeIds.")
    observed_nodes = {name: set() for name in PROCESS_FIELDS}
    node_fields: dict[str, set[str]] = {}
    for records in groups.values():
        for record in records:
            node = record.get("id")
            if isinstance(node, str) and node:
                for name in PROCESS_FIELDS:
                    if name in record:
                        observed_nodes[name].add(node)
                        node_fields.setdefault(node, set()).add(name)
    # A missing configured map is not a licence to splice different producers
    # into a field. Freeze the mapping observed over this export, and mark every
    # affected round if one field changes NodeId or one NodeId supplies two names.
    ambiguous_fields = {name for name, nodes in observed_nodes.items() if len(nodes) > 1}
    for names in node_fields.values():
        if len(names) > 1:
            ambiguous_fields.update(names)
    for stamp, records in sorted(groups.items()):
        row: dict[str, Any] = {"run_id": run_id, "device": device, "time_ns": stamp,
                               "time_utc": timestamp_text(stamp), "temperature_unit": "degC"}
        invalid = set()
        node_records = []
        for record in records:
            selected = [name for name in PROCESS_FIELDS if name in record]
            node_records.append({key: value for key, value in record.items()
                                 if key not in {"result", "table", "_start", "_stop", "_time", "_time_ns",
                                                "time_ns", "time_utc", "_measurement", "device"}})
            for name in selected:
                value, quality, node = record[name], record.get("Quality"), record.get("id")
                if name in row and (row[name] != value or row[f"{name}_node_id"] != node
                                    or row[f"{name}_quality"] != quality):
                    invalid.add(name)
                else:
                    row[name], row[f"{name}_quality"], row[f"{name}_node_id"] = value, quality, node
                good_quality = (type(quality) is int and quality == 0) or (isinstance(quality, str) and quality in GOOD_QUALITY)
                try:
                    finite = type(value) in (int, float) and math.isfinite(value)
                except OverflowError:
                    finite = False
                if (len(selected) != 1 or name in ambiguous_fields or not good_quality or not finite
                        or not isinstance(node, str) or not node
                        or (node_ids.get(name) and node != node_ids[name])):
                    invalid.add(name)
                elif (name in {"c", "count_middle"} and value < 0) or (name in {"T", "T_j", "T_j_set"} and value <= -273.15):
                    invalid.add(name)
        missing = [name for name in PROCESS_FIELDS if name not in row]
        row.update(missing_fields=";".join(missing), invalid_fields=";".join(sorted(invalid)),
                   complete=not missing and not invalid,
                   node_records_json=json.dumps(_json_safe(node_records), ensure_ascii=False, sort_keys=True))
        rows.append(row)
    stamps = sorted(groups)
    # One-and-a-half intervals permits ordinary scheduler jitter. This reports
    # observable gaps; it cannot prove that the source sensor itself resampled.
    tolerance = interval_ns * 3 // 2
    gaps = []
    for left, right in zip([start, *stamps], [*stamps, end]):
        if right - left > tolerance:
            gaps.append({"start_ns": left, "end_ns": right, "duration_ns": right - left})
    complete = sum(bool(row["complete"]) for row in rows)
    coverage.update(device=device, start_ns=start, end_ns=end, interval_ns=interval_ns,
                    rows=len(rows), complete_rounds=complete, incomplete_rounds=len(rows) - complete,
                    first_ns=stamps[0] if stamps else None, last_ns=stamps[-1] if stamps else None,
                    gap_count=len(gaps), gaps=gaps, timestamp_semantics="collection_time",
                    node_mapping="configured" if all(node_ids.get(name) for name in PROCESS_FIELDS) else "observed",
                    observed_node_ids={name: sorted(nodes) for name, nodes in observed_nodes.items()},
                    ambiguous_fields=sorted(ambiguous_fields))
    if not rows:
        coverage["reason"] = "No raw OPC UA collection records were found inside the experiment interval."
    elif complete != len(rows) or gaps:
        coverage.update(status="partial", reason="Raw capture contains missing or invalid nodes, or collection gaps.")
    else:
        coverage.update(status="available", reason="Stored collection rounds cover the interval within the configured sampling tolerance.")
    return rows, coverage


def _lifecycle_events(run_id, manifest):
    for key, kind in (("created_at", "experiment.created"), ("started_at", "experiment.started"),
                      ("ended_at", "experiment.ended")):
        if manifest.get(key):
            stamp = timestamp_ns(manifest[key])
            yield {"run_id": run_id, "time_ns": stamp, "time_utc": timestamp_text(stamp),
                   "measurement": "experiment_manifest", "event_type": kind,
                   "event_id": f"{run_id}:{kind}", "status": manifest.get("status", ""),
                   "error": manifest.get("error") if key == "ended_at" else None}


def _runtime_history_events(run_id, root, stored_events):
    """Reconcile applied Influx events with both durable operator journals.

    Rejected/unconfirmed requests intentionally have no controller_runtime_event
    point; they still belong in the experiment archive. A locally confirmed
    application missing from Influx is exported from its journal, with partial
    coverage, rather than silently omitted or falsely reported persisted.
    """
    local: dict[str, dict] = {}
    conflicts = set()
    for service in ("central", "controller"):
        filename = f".{service}_runtime_history.json"
        for path in (root / filename, root / "_system" / filename):
            document = _read_optional_json(path)
            if document is None:
                continue
            if document.get("schema_version") != 1 or not isinstance(document.get("entries"), list):
                raise ValueError("Invalid runtime history journal schema.")
            for entry in document["entries"]:
                command = entry.get("command")
                if not isinstance(command, dict):
                    raise ValueError("Runtime history entry is missing its command.")
                if command.get("run_id") != run_id:
                    continue
                event_id = command.get("event_id")
                if not isinstance(event_id, str) or not event_id:
                    raise ValueError("Runtime history event_id is missing.")
                result = entry.get("result") or {}
                if not isinstance(result, dict) or (result and (result.get("run_id") != run_id
                                                               or result.get("event_id") != event_id)):
                    raise ValueError("Runtime history result identity mismatch.")
                previous = local.get(event_id)
                sources = set(previous.get("sources", [])) if previous else set()
                sources.add(service)
                if previous:
                    previous_result = previous["entry"].get("result") or {}
                    if previous["entry"]["command"] != command:
                        conflicts.add(event_id)
                    if (previous_result.get("status") in {"applied", "rejected"}
                            and result.get("status") in {"applied", "rejected"}
                            and any(previous_result.get(key) != result.get(key)
                                    for key in ("status", "configuration", "applied_at", "revision"))):
                        conflicts.add(event_id)
                    if previous_result.get("status") in {"applied", "rejected"} and result.get("status") not in {"applied", "rejected"}:
                        previous["sources"] = sorted(sources)
                        continue
                local[event_id] = {"entry": entry, "sources": sorted(sources)}
    stored_by_id = {str(event["event_id"]): event for event in stored_events if event.get("event_id")}
    merged = {str(event.get("event_id") or f"stored:{index}"): dict(event)
              for index, event in enumerate(stored_events)}
    missing, inconsistent, unresolved, untimed, incomplete_configuration = [], [], [], [], []
    for event_id, saved in local.items():
        entry, sources = saved["entry"], saved["sources"]
        command, result = entry["command"], entry.get("result") or {}
        status = result.get("status") or entry.get("status") or "unknown"
        if status not in {"applied", "rejected"}:
            unresolved.append(event_id)
        applied_at = result.get("applied_at") if status == "applied" else None
        occurred_at = applied_at or result.get("processed_at") or command.get("requested_at")
        stamp = timestamp_ns(occurred_at) if occurred_at else None
        if status == "applied" and not applied_at:
            untimed.append(event_id)
        row = {"run_id": run_id, "event_id": event_id, "event_type": "controller.runtime.update",
               "measurement": "runtime_history", "service": "controller" if result else "central",
               "stage": "result" if result else "request", "status": status,
               "time_ns": stamp, "time_utc": timestamp_text(stamp) if stamp is not None else None,
               "timestamp_semantics": "applied_at" if applied_at else (
                   "processed_at" if result.get("processed_at") else "requested_at"),
               "requested_at": command.get("requested_at"), "applied_at": applied_at,
               "before": result.get("before"), "after": result.get("configuration"),
               "changes": command.get("changes"), "no_change": bool(result.get("no_change")),
               "revision": result.get("revision"), "effective_tick": result.get("effective_tick"),
               "error": result.get("reason") or entry.get("error"), "history_sources": sources,
               "payload": entry, "influx_persisted": event_id in stored_by_id}
        expected_influx = status == "applied" and not result.get("no_change")
        if expected_influx and (not isinstance(result.get("before"), dict)
                               or not isinstance(result.get("configuration"), dict)):
            incomplete_configuration.append(event_id)
        if expected_influx and event_id not in stored_by_id:
            missing.append(event_id)
        stored = stored_by_id.get(event_id)
        if stored is not None:
            if (not expected_influx or not applied_at or stored["time_ns"] != timestamp_ns(applied_at)
                    or _mapping_value(stored.get("before")) != result.get("before")
                    or _mapping_value(stored.get("after")) != result.get("configuration")):
                inconsistent.append(event_id)
            # Local Controller outcome is authoritative; retain the stored
            # representation too when it differs so the discrepancy is reviewable.
            row["measurement"] = "controller_runtime_event"
            row["stored_event"] = stored
        merged[event_id] = row
    partial = bool(missing or inconsistent or unresolved or untimed or conflicts or incomplete_configuration)
    coverage = {"status": "partial" if partial else "available", "local_events": len(local),
                "stored_events": len(stored_events), "missing_influx_event_ids": sorted(missing),
                "inconsistent_event_ids": sorted(inconsistent), "unresolved_event_ids": sorted(unresolved),
                "application_time_unavailable_ids": sorted(untimed), "conflicting_history_ids": sorted(conflicts),
                "configuration_unavailable_ids": sorted(incomplete_configuration),
                "reason": ("Runtime history includes unpersisted, inconsistent, unconfirmed, or untimed outcomes."
                           if partial else "Applied runtime events agree with available local journals.")}
    return list(merged.values()), coverage


def _mapping_value(value):
    return json.loads(value) if isinstance(value, str) else value


def _flatten_parameters(value, prefix=""):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _flatten_parameters(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list) and value and all(isinstance(item, dict) and "key" in item for item in value):
        for item in value:
            yield f"{prefix}.{item['key']}", item.get("value", item.get("default"))
    else:
        yield prefix, value


def _parameter_rows(run_id, snapshot, capture, runtime_events, manifest):
    rows = []
    for key, value in _flatten_parameters(snapshot.get("params", snapshot)):
        scope, _, parameter = key.partition(".")
        rows.append({"run_id": run_id, "source": "params_snapshot", "scope": scope if parameter else "",
                     "parameter": parameter or key, "value": value, "effective_at": manifest["started_at"],
                     "parameter_version": snapshot.get("version", manifest.get("parameter_version")),
                     "status": "applied", "is_effective": True})
    for key, value in _flatten_parameters((capture or {}).get("run_configuration", {})):
        rows.append({"run_id": run_id, "source": "run_configuration", "scope": "runtime",
                     "parameter": key, "value": value, "effective_at": manifest["started_at"],
                     "status": "applied", "is_effective": True})
    for event in runtime_events:
        status = event.get("status", "applied")
        if status != "applied" or event.get("no_change"):
            for key, value in _flatten_parameters(event.get("changes") or {}):
                rows.append({"run_id": run_id, "source": "runtime_request", "scope": "runtime",
                             "parameter": key, "value": value, "effective_at": None,
                             "event_id": event.get("event_id"), "status": status, "is_effective": False,
                             "requested_at": event.get("requested_at")})
            continue
        after, before = event.get("after", {}), event.get("before", {})
        after = json.loads(after) if isinstance(after, str) else after
        before = json.loads(before) if isinstance(before, str) else before
        if not isinstance(after, dict) or not isinstance(before, dict):
            if event.get("history_sources"):
                # Legacy journal entries can lack the old or resulting values.
                # Preserve their entire event, but never invent a parameter diff.
                continue
            raise ValueError("Runtime configuration event has invalid before/after values.")
        before_values = dict(_flatten_parameters(before))
        for key, value in _flatten_parameters(after):
            if key in before_values and before_values[key] == value:
                continue
            rows.append({"run_id": run_id, "source": "runtime_change", "scope": "runtime",
                         "parameter": key, "value": value, "effective_at": event.get("applied_at", event["time_utc"]),
                         "event_id": event.get("event_id"), "effective_tick": event.get("effective_tick"),
                         "revision": event.get("revision"), "status": status, "is_effective": True,
                         "requested_at": event.get("requested_at")})
    return rows


def _read_optional_json(path):
    if not path.exists():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Experiment capture metadata must be an object.")
    return result


def _json_safe(value):
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _atomic_csv(path, base_columns, rows):
    columns = [*base_columns, *sorted({key for row in rows for key in row} - set(base_columns))]
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _csv_value(value) for key, value in row.items()})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path, document):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["ARCHIVE_FILENAME", "CAPTURE_FILENAME", "CSV_FILENAMES", "ExperimentExporter",
           "InfluxExportSource", "timestamp_ns", "timestamp_text"]
