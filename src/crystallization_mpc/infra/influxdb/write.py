from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .client import InfluxSettings, create_influx_client, load_influx_settings

try:
    from influxdb_client import Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError as exc:  # pragma: no cover - exercised at runtime
    Point = None
    WritePrecision = None
    SYNCHRONOUS = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


SIGMA_MEASUREMENT = "crystallizer"


def _storage_fields(fields: dict[str, Any], measurement: str) -> dict[str, Any]:
    """Keep fitted activation energy compatible with legacy integer shards.

    Earlier Controller records stored E_A as an integer. InfluxDB fixes field
    types within a shard, so writing a fitted float under that name rejects the
    whole point. A separate, consistently floating field preserves fractional
    values without changing the observation or deleting historical records.
    Readers prefer E_A_float and fall back to the legacy E_A field.
    """
    if measurement != "controller_measurement":
        return fields
    stored = dict(fields)
    activation_energy = stored.pop("E_A", None)
    if activation_energy is not None:
        stored["E_A_float"] = float(activation_energy)
    elif stored.get("E_A_float") is not None:
        stored["E_A_float"] = float(stored["E_A_float"])
    return stored


def _field_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_tagged_point(
    fields: dict[str, Any],
    *,
    tags: dict[str, Any] | None = None,
    measurement: str = SIGMA_MEASUREMENT,
    timestamp: datetime | int | None = None,
) -> Any:
    if Point is None or WritePrecision is None:
        raise RuntimeError("influxdb-client is not installed.") from _IMPORT_ERROR
    if not fields:
        raise RuntimeError("At least one field is required.")

    point_time = timestamp if timestamp is not None else datetime.now(timezone.utc)
    point = Point(measurement).time(point_time, WritePrecision.NS)
    for key, value in (tags or {}).items():
        if value is not None:
            point = point.tag(key, str(value))
    for key, value in _storage_fields(fields, measurement).items():
        if value is not None:
            point = point.field(key, _field_value(value))
    return point


class InfluxWriter:
    def __init__(self, settings: InfluxSettings | None = None) -> None:
        self.settings = settings or load_influx_settings()
        self.client = create_influx_client(self.settings)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

    def write_tagged_fields(
        self,
        fields: dict[str, Any],
        *,
        tags: dict[str, Any] | None = None,
        measurement: str = SIGMA_MEASUREMENT,
        timestamp: datetime | int | None = None,
    ) -> None:
        point = build_tagged_point(
            fields,
            tags=tags,
            measurement=measurement,
            timestamp=timestamp,
        )
        self.write_api.write(bucket=self.settings.bucket, record=point)

    def write_tagged_batch(self, records: list[dict[str, Any]]) -> None:
        """Send an outbox batch in one HTTP request using original point identities."""
        points = [build_tagged_point(record["fields"], tags=record["tags"],
                                    measurement=record["measurement"], timestamp=record["timestamp_ns"])
                  for record in records]
        if points:
            self.write_api.write(bucket=self.settings.bucket, record=points)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "InfluxWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = [
    "InfluxWriter",
    "SIGMA_MEASUREMENT",
    "build_tagged_point",
]
