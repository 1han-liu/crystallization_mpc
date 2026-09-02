from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from crystallization_mpc.infra.influxdb.write import InfluxWriter

GSENSOR_MEASUREMENT = "gsensor_measurement"
GSENSOR_SERVICE_TAG = "gsensor"


@dataclass(frozen=True)
class GsensorMeasurementRecord:
    run_id: str
    frame_seq: int
    image_name: str
    captured_at: str
    processed_at: str
    dt_s: float
    valid: bool
    G_u: float | None
    G_u_KF: float | None
    G_v: float | None
    G_v_KF: float | None
    error: str | None = None
    unit: str = "m/s"
    processing_duration_ms: float | None = None
    u_distance_px: float | None = None
    u_distance_m: float | None = None
    v_distance_px: float | None = None
    v_distance_m: float | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id is required.")
        if int(self.frame_seq) < 1:
            raise ValueError("frame_seq must start at 1.")
        if not self.image_name:
            raise ValueError("image_name is required.")
        if self.unit != "m/s":
            raise ValueError("unit must be 'm/s'.")

    def tags(self) -> dict[str, str]:
        return {
            "service": GSENSOR_SERVICE_TAG,
            "run_id": self.run_id,
            "status": "measured" if self.valid else "invalid",
            "unit": self.unit,
        }

    def fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "frame_seq": int(self.frame_seq),
            "image_name": self.image_name,
            "captured_at": self.captured_at,
            "processed_at": self.processed_at,
            "dt_s": float(self.dt_s),
            "valid": bool(self.valid),
        }
        optional_fields = {
            "G_u": self.G_u,
            "G_u_KF": self.G_u_KF,
            "G_v": self.G_v,
            "G_v_KF": self.G_v_KF,
            "processing_duration_ms": self.processing_duration_ms,
            "u_distance_px": self.u_distance_px,
            "u_distance_m": self.u_distance_m,
            "v_distance_px": self.v_distance_px,
            "v_distance_m": self.v_distance_m,
            "error": self.error,
        }
        fields.update(
            {key: value for key, value in optional_fields.items() if value is not None}
        )
        return fields

    def timestamp(self) -> datetime:
        value = self.processed_at.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


def write_gsensor_measurement(
    writer: InfluxWriter,
    record: GsensorMeasurementRecord,
) -> None:
    writer.write_tagged_fields(
        record.fields(),
        tags=record.tags(),
        measurement=GSENSOR_MEASUREMENT,
        timestamp=record.timestamp(),
    )


__all__ = [
    "GSENSOR_MEASUREMENT",
    "GSENSOR_SERVICE_TAG",
    "GsensorMeasurementRecord",
    "write_gsensor_measurement",
]
