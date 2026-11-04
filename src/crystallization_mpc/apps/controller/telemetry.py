"""InfluxDB record format for Controller calculation results."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
from types import MappingProxyType
from typing import Any

from crystallization_mpc.apps.controller.diagnostics import ControllerDiagnosticsSnapshot
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.process import ProcessState, ProcessWriteResult
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.infra.influxdb.write import InfluxWriter
from crystallization_mpc.apps.controller.numeric import encode_nonfinite

CONTROLLER_MEASUREMENT = "controller_measurement"
CONTROLLER_SERVICE_TAG = "controller"

_DIAGNOSTIC_REASONS = frozenset({
    "unavailable", "unsupported", "invalid_output", "capture_failed",
    "invalid_snapshot", "identity_mismatch", "process_write_failed",
})

# An Influx field has one stable type for every tick/run. Prefix-only checks
# would let a faulty optional adapter turn a counter into text (or invent a
# field) and reject the entire otherwise-valid control measurement.
DIAGNOSTIC_FIELD_TYPES = MappingProxyType({
    **{name: str for name in (
        "diag_snapshot_stage", "diag_control_hold_reason", "diag_T_j_selected_branch",
        "diag_main_lifecycle", "diag_G_lifecycle", "diag_E_A_lifecycle",
        "diag_k_0_lifecycle", "diag_n_lifecycle",
    )},
    **{name: bool for name in (
        "diag_refresh_applied", "diag_physical_limit", "diag_non_increasing_limit",
        "diag_request_physical_limit", "diag_zero_physical_limit",
    )},
    **{name: int for name in (
        "diag_refresh_count", "diag_segment_id", "diag_segment_start_tick",
        "diag_sample_count", "diag_minimum_samples", "diag_fit_count", "diag_fit_failures",
    )},
    **{name: float for name in (
        "diag_main_T_measured", "diag_main_c_measured",
        "diag_int_target", "diag_int_dT_dt", "diag_int_T",
        "diag_dT_dt_request", "diag_T_j_actual", "diag_T_j_selected",
        "diag_main_T_prior", "diag_main_c_prior",
        "diag_main_innovation_T", "diag_main_innovation_c",
        "diag_main_S_T", "diag_main_S_c",
        "diag_G_input", "diag_G_state", "diag_G_prior", "diag_G_innovation_G", "diag_G_S_G",
        "diag_T_j_candidate_raw", "diag_T_j_candidate_bounded", "diag_T_j_zero_raw", "diag_T_j_zero_bounded",
    )},
    **{f"diag_projected_{name}": float for name in ("T", "dT_dt", "c", "dc_dt")},
    **{f"diag_main_{name}_state": float for name in ("T", "dT_dt", "c", "dc_dt")},
    **{f"diag_main_P_{name}{suffix}": float
       for name in ("T", "dT_dt", "c", "dc_dt") for suffix in ("", "_initial")},
    **{f"diag_G_P_{name}{suffix}": float
       for name in ("G", "dG_dt", "d2G_dt2") for suffix in ("", "_initial")},
})

_RAW_FILTER_DIMENSIONS = {"main": (4, 2), "G": (3, 3), "E_A": (3, 3),
                          "k_0": (3, 3), "n": (3, 3)}


def _validate_raw_vector(value: Any, size: int) -> None:
    if (not isinstance(value, list) or len(value) != size
            or any(isinstance(item, bool) or not isinstance(item, (int, float))
                   for item in value)):
        raise ValueError("invalid_snapshot")


def _validate_raw_matrix(value: Any, size: int) -> None:
    if not isinstance(value, list) or len(value) != size:
        raise ValueError("invalid_snapshot")
    for row in value:
        _validate_raw_vector(row, size)


def _validate_raw_filter(value: Any, size: int, observed_size: int) -> None:
    if not isinstance(value, dict):
        raise ValueError("invalid_snapshot")
    if "state" in value:
        _validate_raw_vector(value["state"], size)
    for name in ("covariance", "initial_covariance"):
        if name in value:
            _validate_raw_matrix(value[name], size)
    if "innovation" in value:
        _validate_raw_vector(value["innovation"], observed_size)
    if "innovation_covariance" in value:
        _validate_raw_matrix(value["innovation_covariance"], observed_size)
    for name in ("prior", "posterior"):
        if name in value:
            _validate_raw_filter(value[name], size, observed_size)


def _validate_raw_diagnostics(value: dict[str, Any]) -> None:
    """Validate present containers only; partial legacy snapshots stay valid."""
    for name in ("filters", "lifecycle", "control"):
        if name in value and not isinstance(value[name], dict):
            raise ValueError("invalid_snapshot")
    if "refresh_init" in value and value["refresh_init"] is not None and not isinstance(value["refresh_init"], dict):
        raise ValueError("invalid_snapshot")
    for container in (value.get("filters", {}), value.get("refresh_init") or {}):
        for name, dimensions in _RAW_FILTER_DIMENSIONS.items():
            if name in container:
                _validate_raw_filter(container[name], *dimensions)
    for name in _RAW_FILTER_DIMENSIONS:
        if name in value.get("lifecycle", {}) and not isinstance(value["lifecycle"][name], str):
            raise ValueError("invalid_snapshot")
    control = value.get("control", {})
    for name in ("candidate_raw", "candidate_bounded", "zero_raw", "zero_bounded", "selected"):
        if name in control and control[name] is not None and (
                isinstance(control[name], bool) or not isinstance(control[name], (int, float))):
            raise ValueError("invalid_snapshot")
    for name in ("physical_limit", "request_physical_limit", "zero_physical_limit", "non_increasing_limit"):
        if name in control and control[name] is not None and type(control[name]) is not bool:
            raise ValueError("invalid_snapshot")
    for name in ("hold_reason", "selected_branch"):
        if name in control and not isinstance(control[name], str):
            raise ValueError("invalid_snapshot")


def validate_controller_diagnostics(
    snapshot: ControllerDiagnosticsSnapshot,
    *,
    run_id: str,
    tick: ControllerTickInput,
) -> dict[str, Any]:
    """Validate observational data without letting it alter a control result."""
    if not isinstance(snapshot, ControllerDiagnosticsSnapshot):
        raise ValueError("invalid_snapshot")
    if (snapshot.run_id != run_id or type(snapshot.tick_seq) is not int
            or snapshot.tick_seq != tick.tick_seq
            or isinstance(snapshot.elapsed_s, bool)
            or not isinstance(snapshot.elapsed_s, (int, float))
            or not math.isfinite(float(snapshot.elapsed_s))
            or not math.isclose(float(snapshot.elapsed_s), tick.elapsed_s,
                                rel_tol=0.0, abs_tol=1e-9)):
        raise ValueError("identity_mismatch")
    values = snapshot.fields()
    if not isinstance(values, dict) or not values:
        raise ValueError("invalid_snapshot")
    for name, value in values.items():
        expected_type = DIAGNOSTIC_FIELD_TYPES.get(name) if isinstance(name, str) else None
        if expected_type is None or type(value) is not expected_type:
            raise ValueError("invalid_snapshot")
    raw = snapshot.to_dict()
    if (not isinstance(raw, dict) or raw.get("run_id") != run_id
            or raw.get("tick_seq") != tick.tick_seq
            or raw.get("elapsed_s") != snapshot.elapsed_s
            or encode_nonfinite(raw.get("fields")) != encode_nonfinite(values)
            or not isinstance(raw.get("raw"), dict)):
        raise ValueError("invalid_snapshot")
    _validate_raw_diagnostics(raw["raw"])
    # Complete matrices remain lossless in strict JSON even when the actual
    # calculation produced NaN/Inf. Shape and identity remain independently
    # validated; the codec is representation, not a numerical-input policy.
    json.dumps(encode_nonfinite(raw), allow_nan=False)
    return values


def capture_controller_diagnostics(
    adapter: Any,
    *,
    run_id: str,
    tick: ControllerTickInput,
) -> tuple[ControllerDiagnosticsSnapshot | None, str | None]:
    """Capture once after commit; any observational error is fail-open."""
    try:
        getter = getattr(adapter, "diagnostics_snapshot", None)
        if not callable(getter):
            return None, "unsupported"
        snapshot = getter()
        if snapshot is None:
            return None, "unavailable"
        validate_controller_diagnostics(snapshot, run_id=run_id, tick=tick)
        return snapshot, None
    except ValueError as exc:
        reason = str(exc)
        return None, reason if reason in _DIAGNOSTIC_REASONS else "invalid_snapshot"
    except Exception:
        # Do not expose arbitrary exception text or let diagnostics stop control.
        return None, "capture_failed"


@dataclass(frozen=True)
class ControllerMeasurementRecord:
    run_id: str
    tick: ControllerTickInput
    result: ControllerStepResult
    computed_at: str
    adaptation_enabled: bool
    adaptation_mode: str
    process_state: ProcessState | None = None
    process_write: ProcessWriteResult | None = None
    process_write_attempted: bool = False
    process_write_error: str | None = None
    control_target: str | None = None
    runtime_revision: int = 0
    measurement_parameter_revision: int = 0
    diagnostics: ControllerDiagnosticsSnapshot | None = None
    diagnostics_capture_error: str | None = None
    process_write_requested: bool = False

    def __post_init__(self) -> None:
        if not str(self.run_id).strip():
            raise ValueError("run_id is required.")
        # A successfully received missing-G payload is distinct from a read
        # exception. MATLAB still completes control when adaptation is off;
        # retain the missing fields instead of rejecting its real output.
        if not str(self.computed_at).strip():
            raise ValueError("computed_at is required.")
        if not isinstance(self.adaptation_enabled, bool):
            raise ValueError("adaptation_enabled must be a boolean.")
        if not str(self.adaptation_mode).strip():
            raise ValueError("adaptation_mode is required.")
        for name in ("runtime_revision", "measurement_parameter_revision"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a nonnegative integer.")
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
        tags = {
            "service": CONTROLLER_SERVICE_TAG,
            "run_id": self.run_id,
            "status": "calculated" if self.result.valid else "invalid",
            "adaptation_enabled": str(self.adaptation_enabled).lower(),
            "adaptation_mode": self.adaptation_mode,
        }
        if self.control_target in ("sigma", "G"):
            tags["target"] = self.control_target
        return tags

    def fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "tick_seq": int(self.tick.tick_seq),
            "controller_dt_s": float(self.tick.controller_dt_s),
            "elapsed_s": float(self.tick.elapsed_s),
            "computed_at": self.computed_at,
        }
        sample = self.tick.growth_sample
        if self.tick.growth_sample_age_s is not None:
            fields["growth_sample_age_s"] = float(self.tick.growth_sample_age_s)
        if sample is not None:
            fields.update(
                {
                    "growth_frame_seq": int(sample.frame_seq),
                    "growth_sample_dt_s": float(sample.dt_s),
                    "image_name": sample.image_name,
                    "sample_processed_at": sample.processed_at,
                }
            )
            for name in ("G_u", "G_u_KF", "G_v", "G_v_KF"):
                value = getattr(sample, name)
                if value is not None:
                    fields[f"input_{name}"] = float(value)
            if not sample.valid:
                fields["growth_sample_valid"] = False
            if sample.parameter_revision is not None:
                fields["input_parameter_revision"] = sample.parameter_revision
        fields.update(self.result.fields())
        fields["runtime_revision"] = self.runtime_revision
        fields["measurement_parameter_revision"] = self.measurement_parameter_revision
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
            sources = getattr(self.process_state, "field_sources", None)
            if isinstance(sources, Mapping) and sources:
                # These are the actual inputs used for this calculation, not
                # additional OPC UA acquisitions. Keep last-received times
                # unchanged when a cycle uses a held value.
                fields["process_input_sources"] = json.dumps(
                    encode_nonfinite(dict(sources)), sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                )
                for name in ("T", "T_j", "c", "count_middle", "T_j_set"):
                    source = sources.get(name)
                    if not isinstance(source, Mapping):
                        continue
                    for key in ("updated", "held"):
                        if type(source.get(key)) is bool:
                            fields[f"input_{name}_{key}"] = source[key]
                    for key in ("received_at", "collected_at"):
                        if isinstance(source.get(key), str):
                            fields[f"input_{name}_{key}"] = source[key]
                    error = source.get("last_error")
                    if error is None or isinstance(error, str):
                        fields[f"input_{name}_read_error"] = error or ""
        fields["process_write_requested"] = self.process_write_requested
        fields["process_write_attempted"] = self.process_write_attempted
        fields["process_write_status"] = (
            "success" if self.process_write is not None else
            "failed" if self.process_write_error is not None else
            "pending" if self.process_write_requested else "not_requested"
        )
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
        # Diagnostics add fields to this same point, never a second write. The
        # original process/control fields and status remain independent.
        diagnostic_error = self.diagnostics_capture_error
        try:
            if not self.result.valid:
                diagnostic_error = "invalid_output"
            elif self.process_write_error is not None:
                diagnostic_error = "process_write_failed"
            elif self.diagnostics is not None and diagnostic_error is None:
                fields.update(validate_controller_diagnostics(
                    self.diagnostics, run_id=self.run_id, tick=self.tick
                ))
                fields["diag_capture_ok"] = True
                return fields
            else:
                diagnostic_error = diagnostic_error or "unavailable"
        except Exception:
            diagnostic_error = "invalid_snapshot"
        fields["diag_capture_ok"] = False
        fields["diag_capture_error"] = (
            diagnostic_error if isinstance(diagnostic_error, str)
            and diagnostic_error in _DIAGNOSTIC_REASONS else "capture_failed"
        )
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
    "DIAGNOSTIC_FIELD_TYPES",
    "ControllerMeasurementRecord",
    "write_controller_measurement",
    "capture_controller_diagnostics",
    "validate_controller_diagnostics",
]
