"""Experiment history and CSV reads by run_id, independent of experiment folders.

Only the publisher reads local producer metadata. HTTP readers use InfluxDB
documents and measurements, including when the original folder is unavailable.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import yaml

from crystallization_mpc.experiments.exporter import (
    CSV_FILENAMES, ExperimentExporter, InfluxExportSource, _csv_value, timestamp_ns,
)
from crystallization_mpc.experiments.registry import RUN_ID_PATTERN, ExperimentNotFoundError
from crystallization_mpc.infra.influxdb.client import load_influx_settings, create_influx_client
from crystallization_mpc.infra.influxdb.durable import DurableInfluxWriter, telemetry_path

logger = logging.getLogger(__name__)
MEASUREMENT = "experiment_history_metadata"
CHUNK_SIZE = 16000  # ASCII JSON; even escaped line-protocol strings remain below 64 KiB.
MAX_CHUNKS = 1024


class HistoryUnavailable(RuntimeError):
    pass


def require_run_id(run_id):
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("Invalid experiment run_id.")
    return run_id


def encode_document(document):
    run_id = require_run_id(document.get("run_id"))
    if document.get("schema_version") != 1 or document.get("manifest", {}).get("run_id") != run_id:
        raise ValueError("Invalid experiment history document identity or schema.")
    payload = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    chunks = [payload[i:i + CHUNK_SIZE] for i in range(0, len(payload), CHUNK_SIZE)]
    if len(chunks) > MAX_CHUNKS:
        raise ValueError("Experiment metadata exceeds the supported document size.")
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
    summary = {key: document["manifest"].get(key) for key in (
        "run_id", "label", "status", "created_at", "started_at", "ended_at", "parameter_version", "error")}
    fields = {"schema_version": 1, "chunk_count": len(chunks),
              "summary_json": json.dumps(summary, ensure_ascii=True, allow_nan=False)}
    fields.update({f"part_{i:04d}": chunk for i, chunk in enumerate(chunks)})
    return digest, fields


def decode_document(record, run_id, digest):
    count = record.get("chunk_count")
    if type(count) is not int or not 1 <= count <= MAX_CHUNKS:
        raise HistoryUnavailable("InfluxDB history document has an invalid chunk count.")
    parts = [record.get(f"part_{i:04d}") for i in range(count)]
    if not all(isinstance(part, str) for part in parts):
        raise HistoryUnavailable("InfluxDB history document is incomplete.")
    payload = "".join(parts)
    try:
        payload_bytes = payload.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HistoryUnavailable("InfluxDB history document has invalid encoding.") from exc
    if hashlib.sha256(payload_bytes).hexdigest() != digest:
        raise HistoryUnavailable("InfluxDB history document checksum mismatch.")
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HistoryUnavailable("InfluxDB history document has invalid JSON.") from exc
    if (not isinstance(document, dict) or document.get("schema_version") != 1 or document.get("run_id") != run_id
            or not isinstance(document.get("manifest"), dict) or document["manifest"].get("run_id") != run_id):
        raise HistoryUnavailable("InfluxDB history document identity mismatch.")
    return document


class InfluxHistoryStore:
    def __init__(self, settings=None, *, client_factory=None):
        self.settings = settings or load_influx_settings(prefix="CONTROLLER")
        timeout = int(os.getenv("CENTRAL_HISTORY_QUERY_TIMEOUT_MS", "30000"))
        if not 1 <= timeout <= 120000:
            raise ValueError("CENTRAL_HISTORY_QUERY_TIMEOUT_MS must be between 1 and 120000.")
        self.client_factory = client_factory or (lambda settings: create_influx_client(settings, timeout_ms=timeout))
        self.source = InfluxExportSource(self.settings, client_factory=self.client_factory)

    def _query(self, suffix):
        query = (f'from(bucket: {json.dumps(self.settings.bucket)}) |> range(start: 0) '
                 f'|> filter(fn: (r) => r._measurement == {json.dumps(MEASUREMENT)}) ' + suffix)
        client = None
        try:
            client = self.client_factory(self.settings)
            return [dict(r.values) for t in client.query_api().query(query, org=self.settings.org) for r in t.records]
        except Exception as exc:
            raise HistoryUnavailable("InfluxDB experiment history query failed.") from exc
        finally:
            if client is not None:
                client.close()

    def summaries(self, run_id=None):
        predicate = f'|> filter(fn: (r) => r.run_id == {json.dumps(require_run_id(run_id))}) ' if run_id else ''
        records = self._query(predicate + '|> filter(fn: (r) => r._field == "summary_json") '
                              '|> group(columns: ["run_id"]) |> last()')
        results = []
        for record in records:
            try:
                summary = json.loads(record["_value"])
                if summary.get("run_id") != record.get("run_id"):
                    raise ValueError("Experiment identity mismatch.")
                require_run_id(summary["run_id"])
                timestamp_ns(summary["created_at"])
                results.append((summary, record["content_hash"]))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise HistoryUnavailable("InfluxDB experiment index is invalid.") from exc
        return results

    def get(self, run_id):
        require_run_id(run_id)
        entries = self.summaries(run_id)
        if not entries:
            raise ExperimentNotFoundError(f"No InfluxDB history metadata for {run_id}.")
        _summary, digest = entries[0]
        records = self._query(
            f'|> filter(fn: (r) => r.run_id == {json.dumps(run_id)} and r.content_hash == {json.dumps(digest)}) '
            '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
            '|> sort(columns: ["_time"], desc: true) |> limit(n: 1)')
        if len(records) != 1:
            raise HistoryUnavailable("InfluxDB experiment history document is unavailable.")
        return decode_document(records[0], run_id, digest)


def read_document(registry, directory, manifest, journals, *, metadata_errors=()):
    """Publisher-side snapshot; never called by history/CSV request handlers."""
    run_id = manifest.run_id
    def read_json(filename):
        path = directory / filename
        if not path.exists():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("run_id", run_id) != run_id:
            raise ValueError(f"Invalid experiment identity in {filename}.")
        return document
    snapshot_name = manifest.params_snapshot_file or "params_snapshot.yaml"
    snapshot_path = (directory / snapshot_name).resolve()
    if snapshot_path.parent != directory.resolve():
        raise ValueError("Parameter snapshot must be inside the experiment folder.")
    snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else None
    if snapshot is not None and not isinstance(snapshot, dict):
        raise ValueError("Invalid experiment parameter snapshot.")
    selected = {}
    for service, documents in journals.items():
        selected[service] = []
        for document in documents:
            entries = [entry for entry in document["entries"] if entry.get("command", {}).get("run_id") == run_id]
            selected[service].append({"schema_version": 1, "entries": entries})
    return {"schema_version": 1, "run_id": run_id, "manifest": manifest.to_dict(),
            "capture": read_json("capture.json"), "snapshot": snapshot,
            "finalized": {name: read_json(f"{name}_finalized.json") for name in ("controller", "gsensor")},
            "journals": selected, "metadata_errors": list(metadata_errors)}


def read_journals(root, *, errors=None):
    result = {}
    for service in ("central", "controller"):
        result[service] = []
        for path in (root / f".{service}_runtime_history.json", root / "_system" / f".{service}_runtime_history.json"):
            if path.exists():
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    if (not isinstance(document, dict) or document.get("schema_version") != 1
                            or not isinstance(document.get("entries"), list)
                            or any(not isinstance(entry, dict) or not isinstance(entry.get("command"), dict)
                                   for entry in document["entries"])):
                        raise ValueError("Invalid runtime history journal schema.")
                    result[service].append(document)
                except (OSError, ValueError) as exc:
                    if errors is None:
                        raise
                    errors.append({"code": "runtime_journal_unreadable",
                                   "message": f"{service} runtime journal could not be imported ({type(exc).__name__})."})
    return result


def csv_chunks(base_columns, rows):
    """Same column and value rules as the existing exports, without a CSV file."""
    extras = sorted({key for row in rows for key in row if key not in base_columns})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=[*base_columns, *extras])
    writer.writeheader()
    yield "\ufeff" + stream.getvalue()
    for row in rows:
        stream.seek(0); stream.truncate(0)
        writer.writerow({key: _csv_value(value) for key, value in row.items()})
        yield stream.getvalue()


class DatabaseHistory:
    """Database read API plus a separate metadata publication lane."""
    def __init__(self, manager, *, store=None, writer=None):
        self.manager = manager
        self._store = store
        self._writer = writer
        self._owns_writer = writer is None
        self._hashes = {}
        self._index_loaded = False
        self._stop = threading.Event()
        self._thread = None
        self.error = None
        self.synced_count = 0
        self._source_lock = threading.Lock()

    @property
    def store(self):
        with self._source_lock:
            if self._store is None:
                try:
                    self._store = InfluxHistoryStore()
                except Exception as exc:
                    raise HistoryUnavailable("InfluxDB experiment history is not configured.") from exc
            return self._store

    def _describe(self, manifest):
        run_id = require_run_id(manifest["run_id"])
        result = dict(manifest)
        dashboard = os.getenv("GRAFANA_EXPERIMENT_URL", "http://localhost:3000/d/crystallization-mpc-analysis/crystallization-mpc-analysis")
        args = {"var-run_id": run_id, "to": "now"}
        if manifest.get("started_at"):
            args["from"] = timestamp_ns(manifest["started_at"]) // 1_000_000
        if manifest.get("ended_at"):
            args["to"] = timestamp_ns(manifest["ended_at"]) // 1_000_000
        result.update(source="influxdb", selected=False,
            archive={"status": "database", "errors": [], "pending_reasons": []},
            capture={}, grafana_url=dashboard + ("&" if "?" in dashboard else "?") + urlencode(args),
            downloads={name: f"/api/experiments/{run_id}/exports/{name}" for name in CSV_FILENAMES}
                      if manifest.get("started_at") and manifest.get("ended_at") else {})
        return result

    def history(self, query="", date_from=None, date_to=None):
        if date_from and date_to and date_from > date_to:
            raise ValueError("History start date must not follow its end date.")
        match = query.strip().casefold()
        results = []
        for manifest, _digest in self.store.summaries():
            if match and match not in f'{manifest["run_id"]} {manifest.get("label") or ""}'.casefold():
                continue
            date = manifest["created_at"][:10]
            if (date_from and date < date_from) or (date_to and date > date_to):
                continue
            results.append(self._describe(manifest))
        results.sort(key=lambda item: item["created_at"], reverse=True)
        return {"experiments": results, "source": "influxdb", "error": self.error,
                "export_enabled": True, "metadata_queued_count": self.synced_count}

    def detail(self, run_id):
        document = self.store.get(require_run_id(run_id))
        result = self._describe(document["manifest"])
        result["capture"] = document.get("capture") or {}
        return result

    def collect(self, run_id):
        document = self.store.get(require_run_id(run_id))
        manifest = document["manifest"]
        if not manifest.get("started_at") or not manifest.get("ended_at"):
            raise ValueError("Finish the experiment before downloading its final CSV data.")
        try:
            tables, state = ExperimentExporter(None).collect_database(document, self.store.source)
        except Exception as exc:
            raise HistoryUnavailable("InfluxDB experiment data could not be assembled; check database availability and metadata.") from exc
        state.update(status="partial" if state["errors"] else "completed", source="influxdb",
                     rows={name: len(rows) for name, (_columns, rows) in tables.items()})
        return tables, state

    def export_status(self, run_id):
        _tables, state = self.collect(run_id)
        return {"run_id": run_id, "requested": True, "archive": state,
                "downloads": self.detail(run_id)["downloads"]}

    def download(self, run_id, filename):
        if filename not in CSV_FILENAMES:
            raise ValueError("Unknown experiment CSV file.")
        tables, state = self.collect(run_id)
        columns, rows = tables[filename]
        return csv_chunks(columns, rows), state

    def sync_once(self):
        if self._writer is None:
            self._writer = DurableInfluxWriter(self.store.settings,
                path=telemetry_path("central_history", self.manager.registry.root))
        if not self._index_loaded:
            try:
                for summary, digest in self.store.summaries():
                    self._hashes.setdefault(summary["run_id"], digest)
                self._index_loaded = True
            except HistoryUnavailable:
                pass  # Outbox publication still works while database reads are unavailable.
        metadata_errors = []
        journals = read_journals(self.manager.registry.root, errors=metadata_errors)
        errors = []
        for directory, manifest in self.manager.registry.list_with_directories():
            if self._stop.is_set():
                break
            try:
                document = read_document(self.manager.registry, directory, manifest, journals,
                                         metadata_errors=metadata_errors)
                digest, fields = encode_document(document)
                if self._hashes.get(manifest.run_id) == digest:
                    continue
                self._writer.write_tagged_fields(fields,
                    tags={"run_id": manifest.run_id, "content_hash": digest}, measurement=MEASUREMENT,
                    timestamp=time.time_ns())
                self._hashes[manifest.run_id] = digest
                self.synced_count += 1
            except Exception as exc:
                errors.append(f"{manifest.run_id}: {type(exc).__name__}")
                logger.warning("History metadata publication failed for %s: %s", manifest.run_id, type(exc).__name__)
        messages = [*errors, *(item["message"] for item in metadata_errors)]
        self.error = ("History metadata pending: " + "; ".join(messages)) if messages else None

    def start(self):
        if os.getenv("CENTRAL_HISTORY_PUBLISH_ENABLED", "true").lower() != "true":
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="central-history-metadata", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception as exc:
                self.error = f"InfluxDB history metadata publication unavailable ({type(exc).__name__})."
            self._stop.wait(5)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._writer is not None and self._owns_writer:
            self._writer.close()
