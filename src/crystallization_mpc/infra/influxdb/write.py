from __future__ import annotations

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


def build_point(
    fields: dict[str, float],
    *,
    source: str = "test_sine",
    run_id: str = "test_sigma_001",
    mode: str = "test",
    target: str = "sigma",
    measurement: str = SIGMA_MEASUREMENT,
    timestamp: datetime | None = None,
) -> Any:
    if Point is None or WritePrecision is None:
        raise RuntimeError("influxdb-client is not installed.") from _IMPORT_ERROR
    if not fields:
        raise RuntimeError("At least one field is required.")

    point_time = timestamp or datetime.now(timezone.utc)
    point = (
        Point(measurement)
        .tag("source", source)
        .tag("run_id", run_id)
        .tag("mode", mode)
        .tag("target", target)
        .time(point_time, WritePrecision.NS)
    )
    for key, value in fields.items():
        point = point.field(key, float(value))
    return point


def build_sigma_point(
    sigma: float,
    *,
    source: str = "test_sine",
    run_id: str = "test_sigma_001",
    mode: str = "test",
    target: str = "sigma",
    measurement: str = SIGMA_MEASUREMENT,
    timestamp: datetime | None = None,
) -> Any:
    return build_point(
        {"sigma": sigma},
        source=source,
        run_id=run_id,
        mode=mode,
        target=target,
        measurement=measurement,
        timestamp=timestamp,
    )


class InfluxWriter:
    def __init__(self, settings: InfluxSettings | None = None) -> None:
        self.settings = settings or load_influx_settings()
        self.client = create_influx_client(self.settings)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

    def write_fields(
        self,
        fields: dict[str, float],
        *,
        source: str = "test_sine",
        run_id: str = "test_sigma_001",
        mode: str = "test",
        target: str = "sigma",
        measurement: str = SIGMA_MEASUREMENT,
        timestamp: datetime | None = None,
    ) -> None:
        point = build_point(
            fields,
            source=source,
            run_id=run_id,
            mode=mode,
            target=target,
            measurement=measurement,
            timestamp=timestamp,
        )
        self.write_api.write(bucket=self.settings.bucket, record=point)

    def write_sigma(
        self,
        sigma: float,
        *,
        source: str = "test_sine",
        run_id: str = "test_sigma_001",
        mode: str = "test",
        target: str = "sigma",
        measurement: str = SIGMA_MEASUREMENT,
        timestamp: datetime | None = None,
    ) -> None:
        self.write_fields(
            {"sigma": sigma},
            source=source,
            run_id=run_id,
            mode=mode,
            target=target,
            measurement=measurement,
            timestamp=timestamp,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "InfluxWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def write_sigma(
    sigma: float,
    *,
    source: str = "test_sine",
    run_id: str = "test_sigma_001",
    mode: str = "test",
    target: str = "sigma",
    measurement: str = SIGMA_MEASUREMENT,
    timestamp: datetime | None = None,
) -> None:
    with InfluxWriter() as writer:
        writer.write_sigma(
            sigma,
            source=source,
            run_id=run_id,
            mode=mode,
            target=target,
            measurement=measurement,
            timestamp=timestamp,
        )


__all__ = [
    "InfluxWriter",
    "SIGMA_MEASUREMENT",
    "build_point",
    "build_sigma_point",
    "write_sigma",
]
