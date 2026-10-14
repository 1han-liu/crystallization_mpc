"""Read-only experiment browsing and asynchronous, recoverable CSV export."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from uuid import uuid4

from crystallization_mpc.experiments.exporter import ExperimentExporter, InfluxExportSource, CSV_FILENAMES, timestamp_ns
from crystallization_mpc.experiments.models import TERMINAL_EXPERIMENT_STATUSES, ExperimentStatus

logger = logging.getLogger(__name__)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid archive metadata: {path.name}")
    return value


def read_run_metadata(directory: Path, filename: str, run_id: str) -> dict:
    value = read_json(directory / filename)
    if value and (value.get("run_id") != run_id or value.get("schema_version", 1) != 1):
        raise ValueError(f"Invalid experiment identity or schema in {filename}.")
    return value


class ExperimentArchive:
    def __init__(self, manager, *, controller_status: Callable[[], dict], exporter=None):
        self.manager = manager
        self.registry = manager.registry
        self.controller_status = controller_status
        self.exporter = exporter  # Lazy creation keeps startup independent of InfluxDB.
        self.enabled = os.getenv("EXPERIMENT_EXPORT_ENABLED", "true").lower() == "true"
        self.error: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._requested: set[str] = set()
        self._retry_at: dict[str, float] = {}

    def capture_start(self, run_id: str, configuration: dict, controller: dict) -> None:
        """Fix association before Start is published; never use a later draft."""
        with self._lock:
            path = self.registry.run_dir(run_id) / "capture.json"
            if path.exists():
                return
            manifest = self.registry.get(run_id)
            process = (controller.get("integrations") or {}).get("process") or {}
            enabled = bool(process.get("enabled", os.getenv("CONTROLLER_PROCESS_ENABLED", "false").lower() == "true"))
            interval = os.getenv("TELEGRAF_INTERVAL", "1s")
            import re
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)", interval)
            if not match:
                raise ValueError("TELEGRAF_INTERVAL must be a positive duration in ms, s, m or h.")
            seconds = float(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]
            if seconds <= 0:
                raise ValueError("TELEGRAF_INTERVAL must be positive.")
            atomic_json(path, {
                "schema_version": 1, "run_id": run_id,
                "device": process.get("device_id") or os.getenv("PROCESS_DEVICE_ID", "crystallizer"),
                "node_ids": dict(process.get("node_ids") or {}),
                "start_ns": timestamp_ns(manifest.started_at), "end_ns": None,
                "process_enabled": enabled, "gsensor_enabled": False,
                "write_enabled": bool(process.get("write_enabled", os.getenv("CONTROLLER_PROCESS_WRITE_ENABLED", "false").lower() == "true")),
                "runtime_influx_enabled": os.getenv("CENTRAL_RUNTIME_INFLUX_ENABLED", os.getenv("CONTROLLER_INFLUX_ENABLED", "false")).lower() == "true",
                "telemetry_interval_s": seconds,
                "run_configuration": configuration,
            })

    def note_gsensor_enabled(self, run_id: str) -> None:
        with self._lock:
            path = self.registry.run_dir(run_id) / "capture.json"
            capture = read_json(path)
            if capture and not capture.get("gsensor_enabled"):
                capture["gsensor_enabled"] = True
                atomic_json(path, capture)

    def describe(self, run_id: str, *, manifest=None, directory=None, current_run_id=None) -> dict:
        result = (self.manager.describe(manifest, current_run_id=current_run_id, run_directory=directory)
                  if manifest is not None else self.manager.get(run_id))
        directory = directory or self.registry.run_dir(run_id)
        status = read_run_metadata(directory, "archive.json", run_id) or {"status": "not_exported", "files": {}}
        result["archive"] = status
        result["capture"] = read_run_metadata(directory, "capture.json", run_id)
        result["downloads"] = {
            name: f"/api/experiments/{run_id}/exports/{name}"
            for name in CSV_FILENAMES if status.get("status") in {"completed", "partial"} and (directory / name).is_file()
        }
        dashboard = os.getenv("GRAFANA_EXPERIMENT_URL", "http://localhost:3000/d/crystallization-mpc-analysis/crystallization-mpc-analysis")
        query: dict[str, Any] = {"var-run_id": run_id}
        if result.get("started_at"):
            query["from"] = timestamp_ns(result["started_at"]) // 1_000_000
        if result.get("ended_at"):
            query["to"] = timestamp_ns(result["ended_at"]) // 1_000_000
        else:
            query["to"] = "now"
        result["grafana_url"] = dashboard + ("&" if "?" in dashboard else "?") + urlencode(query)
        return result

    def describe_safely(self, run_id: str, *, manifest=None, directory=None, current_run_id=None) -> dict:
        """Keep optional archive corruption out of live status and other history rows.

        Direct detail/download calls remain strict. This read-only fallback does
        not rewrite evidence, expose stale CSVs or change the current selection.
        """
        try:
            return self.describe(run_id, manifest=manifest, directory=directory, current_run_id=current_run_id)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result = (self.manager.describe(manifest, current_run_id=current_run_id, run_directory=directory)
                      if manifest is not None else self.manager.get(run_id))
            message = (f"Archive metadata is unavailable ({type(exc).__name__}). "
                       "Review the saved experiment files before retrying export.")
            dashboard = os.getenv("GRAFANA_EXPERIMENT_URL", "http://localhost:3000/d/crystallization-mpc-analysis/crystallization-mpc-analysis")
            result.update({
                "archive": {"schema_version": 1, "run_id": run_id, "status": "failed",
                            "files": [], "rows": {}, "pending_reasons": [], "errors": [message]},
                "archive_error": message, "capture": {}, "downloads": {},
                "grafana_url": dashboard + ("&" if "?" in dashboard else "?") + urlencode({"var-run_id": run_id}),
            })
            return result

    def status_summary(self, experiment: dict) -> dict:
        """Read live export status using an already validated registry listing.

        The regular UI poll needs only archive metadata. Re-resolving the run
        and checking every download here needlessly repeat filesystem work,
        especially on Docker host mounts. Capture metadata is still validated
        so a damaged archive remains visible to the operator.
        """
        run_id = experiment["run_id"]
        directory = Path(experiment["experiment_directory"])
        try:
            status = read_run_metadata(directory, "archive.json", run_id)
            read_run_metadata(directory, "capture.json", run_id)
            return {"archive": status or {"status": "not_exported", "files": {}},
                    "archive_error": None}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            message = (f"Archive metadata is unavailable ({type(exc).__name__}). "
                       "Review the saved experiment files before retrying export.")
            return {
                "archive": {"schema_version": 1, "run_id": run_id, "status": "failed",
                            "files": [], "rows": {}, "pending_reasons": [], "errors": [message]},
                "archive_error": message,
            }

    def history(self, query: str = "", date_from: str | None = None, date_to: str | None = None) -> dict:
        match = query.strip().casefold()
        results = []
        current_run_id = self.manager.current_run_id()
        for directory, manifest in self.registry.list_with_directories():
            if match and match not in f"{manifest.run_id} {manifest.label or ''}".casefold():
                continue
            date = manifest.created_at[:10]
            if date_from and date < date_from or date_to and date > date_to:
                continue
            results.append(self.describe_safely(manifest.run_id, manifest=manifest, directory=directory, current_run_id=current_run_id))
        return {"experiments": results, "error": self.error, "export_enabled": self.enabled}

    def request(self, run_id: str) -> dict:
        if self.registry.get(run_id).status not in TERMINAL_EXPERIMENT_STATUSES:
            raise ValueError("End the experiment before exporting its data.")
        with self._lock:
            path = self.registry.run_dir(run_id) / "archive.json"
            atomic_json(path, {"schema_version": 1, "run_id": run_id, "status": "pending",
                              "pending_reasons": ["Export requested. Waiting for final records."],
                              "requested_at_ns": time.time_ns(), "rows": {}, "errors": []})
            self._requested.add(run_id)
            self._retry_at.pop(run_id, None)
            self._wake.set()
        return {"run_id": run_id, "requested": True}

    def download_path(self, run_id: str, filename: str) -> Path:
        if filename not in CSV_FILENAMES:
            raise ValueError("Unknown experiment export file.")
        directory = self.registry.run_dir(run_id)
        if read_run_metadata(directory, "archive.json", run_id).get("status") not in {"completed", "partial"}:
            raise ValueError("CSV downloads are available after export finishes.")
        path = directory / filename
        if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
            raise FileNotFoundError("This CSV has not been exported yet.")
        return path

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="experiment-export", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _readiness(self, manifest, capture: dict) -> list[str]:
        if not capture or not manifest.started_at:
            return []  # Legacy export is explicitly marked incomplete by the exporter.
        directory = self.registry.run_dir(manifest.run_id)
        reasons = []
        final_times = [timestamp_ns(manifest.ended_at)]
        markers = {}
        for component in ("controller", "gsensor"):
            marker = read_json(directory / f"{component}_finalized.json")
            if marker.get("run_id") != manifest.run_id or not marker.get("finalized_at"):
                reasons.append(f"Waiting for {component} finalization.")
            else:
                final_times.append(int(marker.get("finalized_at_ns") or timestamp_ns(marker["finalized_at"])))
                markers[component] = marker
        if reasons:
            return reasons
        if not capture.get("end_ns"):
            capture["end_ns"] = max(final_times)
            atomic_json(directory / "capture.json", capture)
        from crystallization_mpc.infra.influxdb.durable import inspect_archive_queue
        for component in ("controller", "gsensor", "central", "opcua_writer"):
            configured = os.getenv(f"{component.upper()}_TELEMETRY_DB")
            path = Path(configured) if configured else self.registry.root / "_system" / f"{component}_telemetry.sqlite3"
            marker = markers.get(component) or {}
            expected = (marker.get("persistence_enabled") and not marker.get("enqueue_failures")
                        and (marker.get("last_tick", 0) or marker.get("last_frame", 0)))
            expected = expected or (component == "opcua_writer" and capture.get("write_enabled"))
            if path.exists() or expected:
                state = inspect_archive_queue(path, run_id=manifest.run_id)
                if not state.get("available") or state.get("pending_count") or state.get("last_error"):
                    reasons.append(f"Waiting for {component} telemetry persistence.")
        # A final applied runtime change can still be moving from Controller's
        # local history to Central's Influx outbox after the stop acknowledgement.
        central_history = read_json(self.registry.root / "_system" / ".central_runtime_history.json").get("entries", [])
        controller_history = read_json(self.registry.root / "_system" / ".controller_runtime_history.json").get("entries", [])
        synced = {entry["command"]["event_id"] for entry in central_history
                  if entry.get("command", {}).get("run_id") == manifest.run_id and entry.get("grafana_sync") == "synced"}
        for entry in controller_history:
            result = entry.get("result") or {}
            if (capture.get("runtime_influx_enabled", True) and entry.get("command", {}).get("run_id") == manifest.run_id and result.get("status") == "applied"
                    and not result.get("no_change") and result.get("applied_at") and entry["command"]["event_id"] not in synced):
                reasons.append("Waiting for runtime configuration event persistence.")
                break
        if capture.get("process_enabled"):
            from crystallization_mpc.apps.opcua_writer.journal import inspect_writer_outbox
            writer_path = os.getenv("OPCUA_WRITER_AUDIT_DB")
            if writer_path and capture.get("write_enabled"):
                state = inspect_writer_outbox(Path(writer_path), run_id=manifest.run_id)
                if not state.get("available") or state.get("pending_count") or state.get("last_error"):
                    reasons.append("Waiting for OPC UA writer audit persistence.")
            # A later collection reaching this same output establishes that the
            # live stream has advanced beyond this run's capture boundary.
            if not reasons:
                if self.exporter.source is None:
                    self.exporter.source = InfluxExportSource.from_environment()
                later = self.exporter.source.fetch(
                    "crystallizer_raw", device=capture["device"],
                    start_ns=int(capture["end_ns"]), end_ns=time.time_ns(),
                )
                if not any(later):
                    reasons.append("Waiting for raw telemetry after the experiment boundary.")
        return reasons

    def _recover_stopping(self, manifest, directory: Path):
        """Reconcile a lost stop acknowledgement from durable producer evidence.

        These markers are written only after the producer has stopped and queued
        its final observations. They prove termination, not Influx delivery;
        the normal persistence barrier must still run after this transition.
        """
        if manifest.status != ExperimentStatus.STOPPING:
            return manifest
        started_ns = timestamp_ns(manifest.started_at)
        finalized_times = []
        for component, sequence_key in (("controller", "last_tick"), ("gsensor", "last_frame")):
            path = directory / f"{component}_finalized.json"
            if not path.exists():
                return manifest
            if path.is_symlink() or path.resolve().parent != directory.resolve():
                raise ValueError(f"Invalid {component} finalization marker path.")
            marker = read_json(path)
            if marker.get("run_id") != manifest.run_id:
                raise ValueError(f"The {component} finalization marker belongs to another experiment.")
            text = marker.get("finalized_at")
            if not isinstance(text, str) or "T" not in text:
                raise ValueError(f"The {component} finalization marker requires a UTC timestamp.")
            stamp = timestamp_ns(text)
            precise = marker.get("finalized_at_ns", stamp)
            if type(precise) is not int or precise != stamp:
                raise ValueError(f"The {component} finalization timestamps do not match.")
            sequence = marker.get(sequence_key)
            if type(sequence) is not int or sequence < 0:
                raise ValueError(f"The {component} finalization sequence must be a nonnegative integer.")
            if stamp < started_ns or stamp > time.time_ns():
                raise ValueError(f"The {component} finalization timestamp is outside this experiment's lifetime.")
            finalized_times.append(stamp)

        controller = self.controller_status()
        same_run = controller.get("current_run_id") in {None, manifest.run_id}
        if (controller.get("available") and same_run
                and (controller.get("status") == "running" or controller.get("active") is True)):
            return manifest
        seconds, nanos = divmod(max(finalized_times), 1_000_000_000)
        ended_at = datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanos // 1000)
        # Use the producer time, never the later restart/reconciliation time.
        return self.registry.finish(manifest.run_id, now=ended_at)

    def process_once(self) -> None:
        for directory, manifest in self.registry.list_with_directories():
            if self._stop.is_set():
                return
            run_id = manifest.run_id
            try:
                if manifest.status == ExperimentStatus.STOPPING:
                    manifest = self._recover_stopping(manifest, directory)
                if manifest.status not in TERMINAL_EXPERIMENT_STATUSES:
                    continue
                with self._lock:
                    requested = run_id in self._requested
                    status = read_run_metadata(directory, "archive.json", run_id)
                    recover = status.get("status") in {"pending", "exporting", "failed"}
                    # Do not silently backfill unrequested historical experiments.
                    if not requested and not recover and (not self.enabled or not (directory / "capture.json").exists()):
                        continue
                    if not requested and status.get("status") in {"completed", "partial"}:
                        continue
                    if time.monotonic() < self._retry_at.get(run_id, 0):
                        continue
                    self._requested.discard(run_id)
                if self.exporter is None:
                    self.exporter = ExperimentExporter(self.registry)
                with self._lock:
                    capture = read_run_metadata(directory, "capture.json", run_id)
                reasons = self._readiness(manifest, capture)
                self.exporter.export(run_id, ready=not reasons, pending_reasons=reasons, capture=capture or None)
                self.error = None
                self._retry_at[run_id] = time.monotonic() + (2 if reasons else 30)
            except Exception as exc:
                self.error = f"Experiment export failed: {type(exc).__name__}: {exc}"
                logger.exception("Export failed for %s", run_id)
                atomic_json(directory / "archive.json", {
                    "schema_version": 1, "run_id": run_id, "status": "failed", "errors": [self.error],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                self._retry_at[run_id] = time.monotonic() + 10

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception as exc:
                self.error = f"Archive scan failed: {type(exc).__name__}: {exc}"
                logger.exception("Archive scan failed")
            self._wake.wait(2)
            self._wake.clear()
