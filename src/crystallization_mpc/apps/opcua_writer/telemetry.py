"""Transfer journal audit records to the shared archive without device latency."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from crystallization_mpc.infra.influxdb.durable import DurableInfluxWriter, telemetry_path


class WriterAuditRelay:
    def __init__(self, journal_path, *, sink=None, interval_s=0.2):
        self.journal_path = journal_path
        self.sink = sink or DurableInfluxWriter(path=telemetry_path("opcua_writer"))
        self.interval_s = interval_s
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="writer-audit-relay", daemon=True)

    def transfer_once(self, connection):
        rows = connection.execute("SELECT event_id,payload FROM telemetry_outbox WHERE delivered_at_ns IS NULL ORDER BY queued_at_ns LIMIT 100").fetchall()
        for event_id, encoded in rows:
            payload = json.loads(encoded)
            try:
                self.sink.write_tagged_fields(payload["fields"], tags=payload["tags"], measurement=payload["measurement"],
                                              timestamp=payload["timestamp_ns"], event_id=event_id)
            except Exception as exc:
                with connection:
                    connection.execute("UPDATE telemetry_outbox SET last_error=?,attempts=attempts+1 WHERE event_id=?",
                                       (type(exc).__name__, event_id))
                return False
            # The destination committed first. A crash between these two commits
            # resends the same stable event_id, preserving exactly one DB point.
            with connection:
                connection.execute("UPDATE telemetry_outbox SET delivered_at_ns=?,attempts=attempts+1,last_error=NULL WHERE event_id=?",
                                   (time.time_ns(), event_id))
        return bool(rows)

    def start(self):
        self.thread.start()

    def _run(self):
        connection = sqlite3.connect(self.journal_path, timeout=5)
        try:
            while not self.stop_event.is_set():
                try:
                    if self.transfer_once(connection):
                        continue
                except sqlite3.Error:
                    pass  # The committed source rows remain pending for retry.
                self.stop_event.wait(self.interval_s)
        finally:
            connection.close()

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.sink.close()
