"""Drain an existing telemetry outbox without starting any control service.

This can run beside an older producer during a live experiment. Writes retain
the original tags and nanosecond timestamps, making concurrent retry delivery
idempotent. The queue payload and its event identity are never rewritten.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import time

from .client import load_influx_settings
from .durable import DurableInfluxWriter
from .write import InfluxWriter


def _delivery_status(queue: DurableInfluxWriter) -> dict:
    # The full archive status includes timestamps stored alongside large raw
    # payloads. Counting via the existing covering index keeps continuous
    # recovery inexpensive on Windows shared storage.
    queued, pending = queue.connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(delivered_at_ns IS NULL),0) "
        "FROM telemetry_outbox INDEXED BY telemetry_pending_run"
    ).fetchone()
    error = (queue.connection.execute(
        "SELECT last_error FROM telemetry_outbox INDEXED BY telemetry_pending_run "
        "WHERE delivered_at_ns IS NULL AND last_error IS NOT NULL LIMIT 1"
    ).fetchone() if pending else None)
    return {"state": "pending" if pending else "ready", "pending_count": pending,
            "queued_count": queued, "delivered_count": queued - pending,
            "last_error": error[0] if error else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("The existing telemetry database must be present.")
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("The polling interval must be positive.")

    # Fail before opening the outbox if the expected schema is absent. The
    # helper must not silently create a fresh queue at an incorrect mount path.
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as check:
        check.execute("SELECT event_id, payload, delivered_at_ns, attempts FROM telemetry_outbox LIMIT 1")

    queue = DurableInfluxWriter(path=args.database, start_worker=False)
    try:
        settings = load_influx_settings(prefix="CONTROLLER")
        with InfluxWriter(settings) as writer:
            previous = None
            last_report = 0.0
            failed_batches = 0
            while True:
                progressed = queue.flush_once(writer)
                status = _delivery_status(queue)
                failed_batches = 0 if progressed else failed_batches + 1
                summary = {key: status[key] for key in
                           ("state", "pending_count", "queued_count", "delivered_count", "last_error")}
                now = time.monotonic()
                if summary != previous and (not status["pending_count"] or now - last_report >= 10):
                    print(json.dumps(summary), flush=True)
                    previous, last_report = summary, now
                if not args.follow and not progressed:
                    if not status["pending_count"]:
                        return
                    # A failed batch may precede healthy records. Let attempt
                    # ordering expose later batches before declaring failure.
                    if failed_batches >= math.ceil(status["pending_count"] / 100):
                        raise SystemExit("Telemetry remains pending; inspect the reported error type.")
                if not progressed:
                    time.sleep(args.interval)
    finally:
        queue.close()


if __name__ == "__main__":
    main()
