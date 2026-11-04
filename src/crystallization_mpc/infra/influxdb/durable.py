"""Local durable outbox; only its worker is allowed to contact InfluxDB.

A successful enqueue means SQLite committed the observation, not that InfluxDB
accepted it. Rows retain their original timestamp and tags across retries and
restarts, so an ambiguous HTTP result overwrites the same Influx point.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable

from .client import InfluxSettings
from .write import InfluxWriter


def telemetry_path(component: str, root: str | Path | None = None) -> Path:
    override = os.getenv(f"{component.upper()}_TELEMETRY_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    base = Path(root or os.getenv("EXPERIMENT_ROOT", ".runtime/experiments"))
    return base.expanduser().resolve() / "_system" / f"{component}_telemetry.sqlite3"


def timestamp_ns(value: datetime | int | None) -> int:
    if value is None:
        return time.time_ns()
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, datetime):
        raise TypeError("Telemetry timestamp must be an aware datetime or integer nanoseconds.")
    if value.tzinfo is None:
        raise ValueError("Telemetry timestamp must include a timezone.")
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def initialize_outbox(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS telemetry_outbox (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            measurement TEXT NOT NULL,
            timestamp_ns INTEGER NOT NULL,
            payload TEXT NOT NULL,
            queued_at_ns INTEGER NOT NULL,
            delivered_at_ns INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS telemetry_pending_run
        ON telemetry_outbox(run_id, delivered_at_ns);
    """)


def enqueue_record(connection: sqlite3.Connection, fields: dict[str, Any], *,
                   tags: dict[str, Any] | None = None, measurement: str,
                   timestamp: datetime | int | None = None, event_id: str | None = None) -> str:
    """Enqueue inside the caller's transaction; does not commit on its behalf."""
    stamp = timestamp_ns(timestamp)
    payload = {"fields": fields, "tags": tags or {}, "measurement": measurement, "timestamp_ns": stamp}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    identifier = event_id or hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO telemetry_outbox(event_id,run_id,measurement,timestamp_ns,payload,queued_at_ns) "
        "VALUES(?,?,?,?,?,?)",
        (identifier, str((tags or {}).get("run_id", "")), measurement, stamp, encoded, time.time_ns()),
    )
    return identifier


def _queue_status(connection: sqlite3.Connection, run_id: str | None = None) -> dict[str, Any]:
    where, params = (" WHERE run_id=?", (run_id,)) if run_id is not None else ("", ())
    row = connection.execute(
        "SELECT COUNT(*), SUM(delivered_at_ns IS NULL), MAX(delivered_at_ns), MIN(CASE WHEN delivered_at_ns IS NULL THEN queued_at_ns END) "
        "FROM telemetry_outbox" + where, params,
    ).fetchone()
    error_where = where + (" AND " if where else " WHERE ") + "delivered_at_ns IS NULL AND last_error IS NOT NULL"
    error = connection.execute("SELECT last_error FROM telemetry_outbox" + error_where + " ORDER BY queued_at_ns DESC LIMIT 1", params).fetchone()
    queued, pending = int(row[0] or 0), int(row[1] or 0)
    return {"enabled": True, "available": True, "state": "pending" if pending else "ready",
            "pending_count": pending, "queued_count": queued, "delivered_count": queued - pending,
            "last_success_at_ns": row[2], "oldest_pending_at_ns": row[3],
            "last_error": error[0] if error else None}


def inspect_archive_queue(path: str | Path | None, run_id: str | None = None) -> dict[str, Any]:
    """Read an outbox without creating it; missing is not proof of completion."""
    if path is None:
        return {"enabled": False, "available": False, "state": "disabled", "pending_count": None}
    target = Path(path)
    if not target.is_file():
        return {"enabled": True, "available": False, "state": "missing", "pending_count": None}
    try:
        with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            return _queue_status(connection, run_id)
    except sqlite3.Error as exc:
        return {"enabled": True, "available": False, "state": "error", "pending_count": None,
                "last_error": type(exc).__name__}


def mark_finalized(root: str | Path | None, run_id: str, component: str, last_sequence: int,
                   *, enqueue_failures: int = 0, persistence_enabled: bool = True) -> None:
    """Persist the producer's end barrier after its last observation was queued.

    Services used alone in unit/integration tests need not have a registry.
    Filesystem failures for a real experiment propagate, so export cannot claim
    completion when the barrier was not durably saved.
    """
    if not root:
        return
    from crystallization_mpc.experiments.registry import ExperimentRegistry, ExperimentNotFoundError
    try:
        directory = ExperimentRegistry(root).run_dir(run_id)
    except (ExperimentNotFoundError, ValueError):
        return
    path = directory / f"{component}_finalized.json"
    if path.is_file():
        return
    stamp = time.time_ns()
    seconds, nanos = divmod(stamp, 1_000_000_000)
    document = {"run_id": run_id,
                "finalized_at": datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z",
                "finalized_at_ns": stamp, "last_tick" if component == "controller" else "last_frame": last_sequence,
                "enqueue_failures": enqueue_failures, "persistence_enabled": persistence_enabled}
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class DurableInfluxWriter:
    asynchronous = True

    def __init__(self, settings: InfluxSettings | None = None, *, path: str | Path,
                 writer_factory: Callable[[], Any] | None = None, retry_interval_s: float = 1.0,
                 start_worker: bool = True) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        initialize_outbox(self.connection)
        self.connection.commit()
        self._factory = writer_factory or (lambda: InfluxWriter(settings))
        self._retry = retry_interval_s
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._worker, name="influx-outbox", daemon=True)
        if start_worker:
            self._thread.start()

    def write_tagged_fields(self, fields: dict[str, Any], *, tags=None, measurement="crystallizer",
                            timestamp=None, event_id: str | None = None) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("Telemetry outbox is closed.")
            with self.connection:
                identifier = enqueue_record(self.connection, fields, tags=tags, measurement=measurement,
                                            timestamp=timestamp, event_id=event_id)
        self._wake.set()
        return identifier

    def is_delivered(self, event_id: str) -> bool:
        with self._lock:
            row = self.connection.execute("SELECT delivered_at_ns FROM telemetry_outbox WHERE event_id=?", (event_id,)).fetchone()
            return row is not None and row[0] is not None

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            return _queue_status(self.connection, run_id)

    def flush_once(self, writer: Any) -> bool:
        """Deliver one batch, for the worker and deterministic recovery tests."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT event_id,payload FROM telemetry_outbox WHERE delivered_at_ns IS NULL ORDER BY queued_at_ns LIMIT 100"
            ).fetchall()
        for identifier, encoded in rows:
            payload = json.loads(encoded)
            try:
                writer.write_tagged_fields(payload["fields"], tags=payload["tags"],
                                           measurement=payload["measurement"], timestamp=payload["timestamp_ns"])
            except Exception as exc:
                # Client errors may contain tokens and URLs; persist only type.
                with self._lock, self.connection:
                    self.connection.execute("UPDATE telemetry_outbox SET attempts=attempts+1,last_error=? WHERE event_id=?",
                                            (type(exc).__name__, identifier))
                return False
            with self._lock, self.connection:
                self.connection.execute("UPDATE telemetry_outbox SET delivered_at_ns=?,attempts=attempts+1,last_error=NULL WHERE event_id=?",
                                        (time.time_ns(), identifier))
        return bool(rows)

    def _worker(self) -> None:
        writer = None
        try:
            while not self._stop.is_set():
                try:
                    writer = writer or self._factory()
                    if self.flush_once(writer):
                        continue
                except Exception as exc:
                    with self._lock, self.connection:
                        self.connection.execute("UPDATE telemetry_outbox SET last_error=? WHERE delivered_at_ns IS NULL", (type(exc).__name__,))
                self._wake.wait(self._retry)
                self._wake.clear()
        finally:
            if writer is not None:
                writer.close()
            with self._lock:
                if self._closed:
                    self.connection.close()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._closed = True
        if self._thread.is_alive():
            self._thread.join(timeout=1)
        elif self._thread.ident is None:
            self.connection.close()


__all__ = ["DurableInfluxWriter", "telemetry_path", "inspect_archive_queue", "enqueue_record", "initialize_outbox"]
