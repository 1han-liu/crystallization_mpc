"""InfluxDB record format for translated Controller calculation results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.process import ProcessState, ProcessWriteResult
from crystallization_mpc.infra.influxdb.write import InfluxWriter
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload

CONTROLLER_MEASUREMENT = "controller_measurement"
CONTROLLER_SERVICE_TAG = "controller"


@dataclass(frozen=True)
class ControllerMeasurementRecord:
    sample: GrowthRateSamplePayload
    result: ControllerStepResult
    computed_at: str
    adaptation_enabled: bool
    adaptation_mode: str
    process_state: ProcessState | None = None
    process_write: ProcessWriteResult | None = None
    process_write_attempted: bool = False
    process_write_error: str | None = None

    def __post_init__(self) -> None:
        if not self.sample.valid:
            raise ValueError("Controller measurement requires a valid Gsensor sample.")
        if not str(self.computed_at).strip():
            raise ValueError("computed_at is required.")
        if not isinstance(self.adaptation_enabled, bool):
            raise ValueError("adaptation_enabled must be a boolean.")
        if not str(self.adaptation_mode).strip():
            raise ValueError("adaptation_mode is required.")
        if not isinstance(self.process_write_attempted, bool):
            raise ValueError("process_write_attempted must be a boolean.")
        if self.process_write is not None and not self.process_write_attempted:
            raise ValueError("A process write result requires an attempted write.")
        if self.process_write_error is not None:
            if not self.process_write_attempted:
                raise ValueError("A process write error requires an attempted write.")
            if not str(self.process_write_error).strip():
                raise ValueError("process_write_error cannot be empty.")

    def tags(self) -> dict[str, str]:
        return {
            "service": CONTROLLER_SERVICE_TAG,
            "run_id": self.sample.run_id,
            "status": "calculated" if self.result.valid else "invalid",
            "adaptation_enabled": str(self.adaptation_enabled).lower(),
            "adaptation_mode": self.adaptation_mode,
        }

    def fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "frame_seq": int(self.sample.frame_seq),
            "image_name": self.sample.image_name,
            "sample_processed_at": self.sample.processed_at,
            "computed_at": self.computed_at,
            "input_G_u": float(self.sample.G_u),
            "input_G_u_KF": float(self.sample.G_u_KF),
            "input_G_v": float(self.sample.G_v),
            "input_G_v_KF": float(self.sample.G_v_KF),
        }
        fields.update(self.result.fields())
        if self.process_state is not None:
            fields.update(
                {
                    "process_T": float(self.process_state.T),
                    "process_T_j": float(self.process_state.T_j),
                    "process_c": float(self.process_state.c),
                    "process_count_middle": float(self.process_state.count_middle),
                    "process_T_j_set_read": float(self.process_state.T_j_set),
                    "process_read_at": self.process_state.read_at,
                }
            )
        fields["process_write_attempted"] = self.process_write_attempted
        if self.process_write is not None:
            fields.update(
                {
                    "process_write_success": True,
                    "process_T_j_set_written_K": float(
                        self.process_write.setpoint_K
                    ),
                    "process_T_j_set_written_C": float(
                        self.process_write.setpoint_C
                    ),
                    "process_write_verified": self.process_write.verified,
                    "process_written_at": self.process_write.written_at,
                }
            )
            if self.process_write.readback_K is not None:
                fields["process_T_j_set_readback_K"] = float(
                    self.process_write.readback_K
                )
        elif self.process_write_error is not None:
            fields["process_write_success"] = False
            fields["process_write_error"] = self.process_write_error
        return fields

    def timestamp(self) -> datetime:
        value = self.computed_at.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


def write_controller_measurement(
    writer: InfluxWriter,
    record: ControllerMeasurementRecord,
) -> None:
    writer.write_tagged_fields(
        record.fields(),
        tags=record.tags(),
        measurement=CONTROLLER_MEASUREMENT,
        timestamp=record.timestamp(),
    )


__all__ = [
    "CONTROLLER_MEASUREMENT",
    "CONTROLLER_SERVICE_TAG",
    "ControllerMeasurementRecord",
    "write_controller_measurement",
]
