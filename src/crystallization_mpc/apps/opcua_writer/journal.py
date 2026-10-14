"""SQLite deduplication and ordering, committed before the device is touched."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from crystallization_mpc.infra.influxdb.durable import initialize_outbox, enqueue_record, inspect_archive_queue
from crystallization_mpc.infra.influxdb.process_events import process_event

from .commands import WriteCommand


class CommandJournal:
    """A single writer owns this database for the lifetime of the service.

    An interrupted execution is deliberately terminal and uncertain. Replaying
    its command returns `unknown`, including when the device did apply it.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT
            );
            CREATE TABLE IF NOT EXISTS watermark (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                run_id TEXT NOT NULL,
                run_started_at REAL NOT NULL,
                tick_seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiments (
                run_id TEXT PRIMARY KEY,
                run_started_at REAL NOT NULL
            );
            """
        )
        initialize_outbox(self.connection)
        self.recovered_count = self._recover_executing()

    def _recover_executing(self) -> int:
        rows = self.connection.execute("SELECT * FROM commands WHERE state='executing'").fetchall()
        with self.connection:
            for row in rows:
                payload = json.loads(row["payload"])
                result = {
                    key: payload[key]
                    for key in ("command_id", "run_id", "run_started_at", "tick_seq")
                }
                result.update(
                    status="unknown",
                    error="Writer restarted during execution; the device outcome is unknown. Command will not be replayed.",
                    write_result=None,
                )
                self._save_result(row["command_id"], result)
        return len(rows)

    def reserve(self, command: WriteCommand, *, rejection: str | None = None) -> dict[str, Any] | None:
        """Return a terminal result, or durably reserve exactly one attempt."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT fingerprint, state, result FROM commands WHERE command_id=?", (command.command_id,)
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != command.fingerprint():
                    return command.result("failed", "command_id was already used for a different command.")
                if existing["result"]:
                    return json.loads(existing["result"])
                result = command.result("unknown", "Command execution is already in progress; it will not be replayed.")
                self._save_result(command.command_id, result)
                return result

            experiment = self.connection.execute(
                "SELECT run_started_at FROM experiments WHERE run_id=?", (command.run_id,)
            ).fetchone()
            if experiment is not None and experiment["run_started_at"] != command.run_started_at.timestamp():
                rejection = "run_started_at changed for a previously known experiment."
            latest = self.connection.execute("SELECT * FROM watermark WHERE singleton=1").fetchone()
            if latest is not None and rejection is None:
                start = command.run_started_at.timestamp()
                if start < latest["run_started_at"]:
                    rejection = "Command belongs to an older experiment."
                elif start == latest["run_started_at"]:
                    if command.run_id != latest["run_id"]:
                        rejection = "Different experiment has the same start timestamp."
                    elif command.tick_seq <= latest["tick_seq"]:
                        rejection = "Command belongs to an old or already attempted control cycle."
                elif command.run_id == latest["run_id"]:
                    rejection = "run_started_at changed for the same experiment."

            self.connection.execute(
                "INSERT INTO commands(command_id, fingerprint, state, payload) VALUES (?, ?, 'executing', ?)",
                (command.command_id, command.fingerprint(), command.fingerprint()),
            )
            if rejection:
                result = command.result("failed", rejection)
                self._save_result(command.command_id, result)
                return result
            self.connection.execute(
                "INSERT OR REPLACE INTO watermark(singleton, run_id, run_started_at, tick_seq) VALUES (1, ?, ?, ?)",
                (command.run_id, command.run_started_at.timestamp(), command.tick_seq),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO experiments(run_id, run_started_at) VALUES (?, ?)",
                (command.run_id, command.run_started_at.timestamp()),
            )
            self._audit(json.loads(command.fingerprint()), "executing")
        return None

    def finish(self, command_id: str, result: dict[str, Any]) -> None:
        with self.connection:
            self._save_result(command_id, result)

    def _save_result(self, command_id: str, result: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE commands SET state=?, result=? WHERE command_id=?",
            (result["status"], json.dumps(result, allow_nan=False), command_id),
        )
        self._audit(result, "result")

    def _audit(self, payload: dict[str, Any], stage: str) -> None:
        enqueue_record(self.connection, **process_event(payload, "opcua_writer", stage))

    def audit_rejection(self, result: dict[str, Any]) -> None:
        with self.connection:
            self._audit(result, "invalid")

    def close(self) -> None:
        self.connection.close()


def inspect_writer_outbox(path: str | Path | None, run_id: str | None = None):
    """Pending here means journal events not yet handed to the durable archive."""
    return inspect_archive_queue(path, run_id)
