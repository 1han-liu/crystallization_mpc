"""Read-only proof that actual Telegraf collections reached isolated InfluxDB.

The comparison retains the RFC3339 nanoseconds from annotated CSV. Converting
query results to Python datetime first would discard sub-microsecond identity.
No database or RabbitMQ measurement is generated or rewritten by this module.
"""
from __future__ import annotations

import calendar
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from crystallization_mpc.apps.controller.rabbit_process import FIELDS, GOOD_QUALITY_STRINGS
from crystallization_mpc.infra.influxdb.client import InfluxSettings, create_influx_client


def timestamp_ns(value: str) -> int:
    """Preserve all nine fractional digits; never pass through floating time."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    if not match:
        raise ValueError("Expected a timezone-qualified RFC3339 timestamp with at most 9 fractional digits.")
    whole, fraction, offset = match.groups()
    stamp = datetime.fromisoformat(whole + ("+00:00" if offset == "Z" else offset)).astimezone(timezone.utc)
    return calendar.timegm(stamp.utctimetuple()) * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def load_test_influx_settings(path: Path) -> InfluxSettings:
    """Explicit test credentials only: never fall back to the production .env."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or any(not isinstance(document.get(key), str) or not document[key].strip()
                                                for key in ("url", "org", "bucket", "token")):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ValueError("A readable test telemetry configuration with url/org/bucket/token is required.") from None
    return InfluxSettings(**{key: document[key] for key in ("url", "org", "bucket", "token")})


def parse_raw_csv(payload: str) -> list[dict[str, Any]]:
    rows = []
    types: list[str] = []
    header: list[str] | None = None
    defaults: list[str] = []
    for line in csv.reader(io.StringIO(payload)):
        if not line:
            continue
        if line[0] == "#datatype":
            types = line
            header = None
            defaults = []
            continue
        if line[0] == "#default":
            defaults = line
            continue
        if line[0].startswith("#"):
            continue
        if "_time" in line and "_field" in line and "_value" in line:
            header = line
            if len(types) != len(header):
                raise ValueError("Influx annotated CSV is missing matching datatype metadata.")
            continue
        if header is None or len(line) != len(header):
            raise ValueError("Influx annotated CSV has an invalid header/data row.")
        row = {name: (line[index] if line[index] != "" else (defaults[index] if index < len(defaults) else ""))
               for index, name in enumerate(header) if name}
        value_type = types[header.index("_value")]
        if value_type == "double":
            value: Any = float(row["_value"])
            if not math.isfinite(value):
                value = row["_value"]  # Preserve invalid literal without putting NaN/Inf in JSON evidence.
        elif value_type in {"long", "unsignedLong"}:
            value = int(row["_value"])
        elif value_type == "string":
            value = row["_value"]
        else:
            raise ValueError(f"Unexpected raw measurement datatype: {value_type}.")
        row["_value"] = value
        row["_value_datatype"] = value_type
        row["collection_timestamp_ns"] = timestamp_ns(row["_time"])
        rows.append(row)
    return rows


def _raw_response_text(response: Any) -> str:
    """Influx client versions return text, bytes, or an HTTPResponse stream."""
    if isinstance(response, str):
        return response
    if isinstance(response, (bytes, bytearray)):
        return bytes(response).decode("utf-8")
    read = getattr(response, "read", None)
    if not callable(read):
        raise ValueError("Influx raw query returned an unsupported response type.")
    try:
        # urllib3 caches a preloaded body in .data; .read() can then return
        # None/empty. The .data property also consumes an unbuffered response.
        body = getattr(response, "data", None)
        if body is None:
            body = read()
        if isinstance(body, str):
            return body
        if isinstance(body, (bytes, bytearray)):
            return bytes(body).decode("utf-8")
        raise ValueError("Influx raw query response body is not CSV text or bytes.")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


class RawStoreVerificationError(RuntimeError):
    pass


class RawStoreVerifier:
    def __init__(self, settings: InfluxSettings, node_ids: Mapping[str, str], *,
                 device_id: str = "crystallizer", measurement_name: str = "crystallizer_raw",
                 query_raw: Callable[[str], Any] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if set(node_ids) != set(FIELDS) or len(set(node_ids.values())) != 5:
            raise ValueError("Raw storage verification requires five distinct configured NodeIds.")
        self.settings, self.node_ids = settings, dict(node_ids)
        self.device_id, self.measurement_name = device_id, measurement_name
        self._query_raw, self.clock, self.sleep = query_raw, monotonic, sleep
        self.last_evidence: dict[str, Any] = {}

    def _query(self, first: int, last: int) -> str:
        literal = json.dumps
        return (f'from(bucket: {literal(self.settings.bucket)})\n'
                f' |> range(start: time(v: {first}), stop: time(v: {last + 1}))\n'
                f' |> filter(fn: (r) => r._measurement == {literal(self.measurement_name)} and r.device == {literal(self.device_id)})\n'
                f' |> filter(fn: (r) => contains(value: r.id, set: {literal(list(self.node_ids.values()))}))\n'
                f' |> filter(fn: (r) => contains(value: r._field, set: {literal([*FIELDS, "Quality"])}))\n'
                ' |> keep(columns: ["_measurement", "_time", "device", "id", "_field", "_value"])')

    @staticmethod
    def _captured_rounds(captured_rounds: Sequence[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
        rounds: dict[int, Mapping[str, Any]] = {}
        for item in captured_rounds:
            stamp = timestamp_ns(item["read_at"])
            if item.get("collection_timestamp_ns", stamp) != stamp or stamp in rounds:
                raise ValueError("Captured Rabbit rounds must have unique, exact nanosecond identities.")
            if any(field not in item or isinstance(item[field], bool) or not math.isfinite(float(item[field])) for field in FIELDS):
                raise ValueError("Captured Rabbit round is incomplete/nonfinite.")
            rounds[stamp] = item
        if not rounds:
            raise ValueError("At least one actual Rabbit collection is required.")
        return rounds

    def compare_records(self, records: Sequence[Mapping[str, Any]],
                        captured_rounds: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Pure, strict evidence revalidation; no network or database access.

        Callers importing a commissioning report must additionally compare the
        reported database and server identities with their expected deployment.
        This method verifies the independently captured raw rows themselves.
        """
        rounds = self._captured_rounds(captured_rounds)
        indexed: dict[tuple[int, str, str], Mapping[str, Any]] = {}
        relevant = []
        field_by_node = {node_id: field for field, node_id in self.node_ids.items()}
        for record in records:
            stamp = record["collection_timestamp_ns"]
            if type(stamp) is not int or timestamp_ns(record["_time"]) != stamp:
                raise RawStoreVerificationError("Raw row timestamp does not preserve its exact collection nanoseconds.")
            if stamp not in rounds:
                continue
            if record.get("_measurement") != self.measurement_name or record.get("device") != self.device_id:
                raise RawStoreVerificationError("Influx result contains another measurement/device at a target timestamp.")
            expected_field = field_by_node.get(record.get("id"))
            if expected_field is None or record.get("_field") not in {expected_field, "Quality"}:
                raise RawStoreVerificationError("Influx result contains an unexpected NodeId/field pairing.")
            key = stamp, record.get("id"), record["_field"]
            if key in indexed:
                raise RawStoreVerificationError("Duplicate raw point identity; extra tag series cannot be silently merged.")
            indexed[key] = record
            relevant.append(dict(record))
        missing, mismatch = [], []
        values_matched = quality_matched = 0
        for stamp, expected in rounds.items():
            for field in FIELDS:
                node_id = self.node_ids[field]
                row = indexed.get((stamp, node_id, field))
                quality = indexed.get((stamp, node_id, "Quality"))
                if row is None:
                    missing.append({"collection_timestamp_ns": stamp, "id": node_id, "field": field})
                else:
                    expected_value = float(expected[field]) - (273.15 if field in {"T", "T_j", "T_j_set"} else 0)
                    value = row["_value"]
                    if row["_value_datatype"] != "double" or not isinstance(value, float) or not math.isfinite(value) or not math.isclose(value, expected_value, rel_tol=0, abs_tol=1e-8):
                        mismatch.append({"collection_timestamp_ns": stamp, "id": node_id, "field": field,
                                         "expected_raw_value": expected_value, "actual": value,
                                         "actual_datatype": row["_value_datatype"]})
                    else:
                        values_matched += 1
                if quality is None:
                    missing.append({"collection_timestamp_ns": stamp, "id": node_id, "field": "Quality"})
                else:
                    value, datatype = quality["_value"], quality["_value_datatype"]
                    good = ((datatype == "string" and value in GOOD_QUALITY_STRINGS)
                            or (datatype in {"long", "unsignedLong"} and type(value) is int and value == 0))
                    if not good:
                        mismatch.append({"collection_timestamp_ns": stamp, "id": node_id, "field": "Quality",
                                         "actual": value, "actual_datatype": datatype})
                    else:
                        quality_matched += 1
        return {"matched_value_count": values_matched, "matched_quality_count": quality_matched,
                "missing": missing, "mismatches": mismatch, "records": relevant}

    def verify(self, captured_rounds: Sequence[Mapping[str, Any]], *, timeout_s: float = 30) -> dict[str, Any]:
        rounds = self._captured_rounds(captured_rounds)
        query = self._query(min(rounds), max(rounds))
        self.last_evidence = {
            "source": "InfluxDB raw query", "url": self.settings.url, "org": self.settings.org,
            "bucket": self.settings.bucket, "measurement": self.measurement_name,
            "device_id": self.device_id, "node_ids": self.node_ids, "round_count": len(rounds),
            "collection_timestamp_ns": sorted(rounds), "query": query, "status": "PENDING",
        }
        client = None
        query_raw = self._query_raw
        try:
            if query_raw is None:
                client = create_influx_client(self.settings)
                query_raw = lambda q: client.query_api().query_raw(q, org=self.settings.org)
            deadline = self.clock() + timeout_s
            while True:
                payload = _raw_response_text(query_raw(query))
                comparison = self.compare_records(parse_raw_csv(payload), captured_rounds)
                self.last_evidence.update(comparison)
                self.last_evidence["query_response_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
                if comparison["mismatches"]:
                    raise RawStoreVerificationError("Influx raw data differs in value, datatype, or OPC UA quality from captured Rabbit collections.")
                if not comparison["missing"]:
                    self.last_evidence["status"] = "PASS"
                    return dict(self.last_evidence)
                if self.clock() >= deadline:
                    raise RawStoreVerificationError(f'Influx persistence timeout: {len(comparison["missing"])} raw fields are absent at exact collection timestamps.')
                self.sleep(min(1, max(0, deadline - self.clock())))
        except Exception as exc:
            self.last_evidence["status"] = "FAIL"
            # Never include client settings, HTTP headers, or credentials in evidence.
            safe_message = str(exc) if isinstance(exc, (RawStoreVerificationError, ValueError)) else type(exc).__name__
            self.last_evidence["error"] = safe_message.replace(self.settings.token, "[redacted]")
            raise RawStoreVerificationError(self.last_evidence["error"]) from None
        finally:
            if client is not None:
                client.close()


__all__ = ["RawStoreVerifier", "RawStoreVerificationError", "timestamp_ns", "load_test_influx_settings"]
