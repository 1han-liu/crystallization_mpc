"""RabbitMQ-facing Controller lifecycle and input validation service."""

from __future__ import annotations

from crystallization_mpc.infra.runtime_history import RuntimeHistory

import copy
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from crystallization_mpc.apps.controller.adapter import (
    ControllerAdapter,
    NoOpControllerAdapter,
    load_controller_adapter,
)
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.apps.controller.process import (
    NoOpProcessAdapter,
    OpcUaNodeConfig,
    OpcUaProcessAdapter,
    OpcUaProcessConfig,
    ProcessAdapter,
    ProcessState,
    ProcessWriteResult,
)
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.apps.controller.telemetry import (
    ControllerMeasurementRecord,
    write_controller_measurement,
)
from crystallization_mpc.infra.influxdb.client import InfluxSettings
from crystallization_mpc.infra.influxdb.write import InfluxWriter
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.commands import (
    CONTROLLER_ADD_SEED_COMMAND,
    CONTROLLER_ADAPTATION_SET_COMMAND,
    CONTROLLER_RUNTIME_UPDATE_COMMAND,
    EXPERIMENT_START_COMMAND,
    EXPERIMENT_STOP_COMMAND,
    GSENSOR_DISABLE_COMMAND,
    GSENSOR_ENABLE_COMMAND,
    GROWTH_RATE_SAMPLE_MESSAGE,
    GROWTH_RATE_STATUS_MESSAGE,
    PARAMS_UPDATE_MESSAGE,
)
from crystallization_mpc.messaging.contracts import (
    ControllerAddSeedPayload,
    ControllerAdaptationPayload,
    ExperimentStartPayload,
    ExperimentStopPayload,
    GrowthRateStatusPayload,
    GrowthRateSamplePayload,
    GsensorActivationPayload,
)
from crystallization_mpc.messaging.routing import bindings_for
from crystallization_mpc.messaging.controller_runtime import (
    ControllerRuntimeUpdatePayload, validate_runtime_changes,
)
from crystallization_mpc.messaging.schema import utc_ts

ROLE = "controller"
CONTROLLER_STATE_FILENAME = ".controller_runtime_state.json"
logger = logging.getLogger(__name__)


class ControllerState(str, Enum):
    IDLE = "idle"
    CONFIGURED = "configured"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


ConsumerRunner = Callable[..., None]


class ControllerService:
    """Validate bus messages and forward only valid samples to an adapter."""

    def __init__(
        self,
        settings: ControllerSettings | None = None,
        adapter: ControllerAdapter | None = None,
        consumer_runner: ConsumerRunner = start_consumer,
        measurement_writer: InfluxWriter | None = None,
        process_adapter: ProcessAdapter | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or ControllerSettings.from_env()
        self.adapter = adapter or load_controller_adapter(self.settings.adapter_spec)
        self.process_adapter = process_adapter or self._build_process_adapter()
        self._consumer_runner = consumer_runner
        self._consumer_thread: threading.Thread | None = None
        self._consumer_stop = threading.Event()
        self._control_thread: threading.Thread | None = None
        self._control_stop = threading.Event()
        self._control_wakeup = threading.Event()
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()

        self.state = ControllerState.IDLE
        self.consumer_status = "not_started"
        self.parameters: dict[str, Any] = {}
        self.parameter_version: int | None = None
        self.current_run_id: str | None = None
        self.started_at: str | None = None
        self.stopped_at: str | None = None
        self.last_frame_seq: int | None = None
        self.last_sample: dict[str, Any] | None = None
        self.last_valid_sample: GrowthRateSamplePayload | None = None
        self.last_valid_sample_received_monotonic: float | None = None
        self.last_valid_sample_arrival_age_s: float | None = None
        # A running live experiment starts with GSensor explicitly disabled.
        # The cache remains unusable until Central's ordered activation command,
        # a matching GSensor status acknowledgement, and a fresh valid sample.
        self.gsensor_desired_enabled = False
        self.gsensor_enabled = False
        self.gsensor_measurement_ready = False
        self.gsensor_control_revision = 0
        self.gsensor_control_event_id: str | None = None
        self.gsensor_last_command_enabled: bool | None = None
        self.gsensor_last_command_requested_at: str | None = None
        self.gsensor_initialization_generation = 0
        self.gsensor_alignment_revision = 0
        self.gsensor_sample_floor_frame_seq = 0
        self.gsensor_transition_occurred_at: str | None = None
        self.gsensor_last_status_at: str | None = None
        self.gsensor_gate_reason = "no_experiment"
        self.control_tick_count = 0
        self.control_tick_error_count = 0
        self.last_growth_sample_age_s: float | None = None
        self._run_started_monotonic: float | None = None
        self._next_tick_monotonic: float | None = None
        self.last_message: dict[str, Any] | None = None
        self.last_message_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.received_message_count = 0
        self.accepted_message_count = 0
        self.rejected_message_count = 0
        self.valid_sample_count = 0
        self.invalid_sample_count = 0
        self.duplicate_sample_count = 0
        self.missing_frame_count = 0
        self.adapter_error_count = 0
        self.process_error_count = 0
        self.last_process_error: str | None = None
        self.last_process_state: dict[str, Any] | None = None
        self.last_process_write: dict[str, Any] | None = None
        self.last_control_output: dict[str, Any] | None = None
        self.control_output_count = 0
        self.invalid_control_output_count = 0
        self.influx_write_success_count = 0
        self.influx_write_failure_count = 0
        self.last_influx_write_at: str | None = None
        self.last_influx_error: str | None = None
        self.influx_initialization_error: str | None = None
        self.measurement_writer = measurement_writer
        self._measurement_writer_closed = False
        if self.measurement_writer is None and self.settings.influx_enabled:
            try:
                if not self.settings.influx_token:
                    raise RuntimeError("INFLUX_TOKEN is required for Controller persistence.")
                self.measurement_writer = InfluxWriter(
                    InfluxSettings(
                        url=self.settings.influx_url,
                        token=self.settings.influx_token,
                        org=self.settings.influx_org,
                        bucket=self.settings.influx_bucket,
                    )
                )
            except Exception as exc:
                self.influx_initialization_error = str(exc)
                self.last_influx_error = str(exc)
                logger.warning("Could not initialize Controller InfluxDB writer: %s", exc)
        self.seed_event_count = 0
        self.duplicate_seed_event_count = 0
        self.last_seed_event: dict[str, Any] | None = None
        self._processed_seed_event_ids: set[str] = set()
        self.adaptation_enabled = False
        self.adaptation_mode = "E_A"
        self.last_fit_success_at: str | None = None
        self.adaptation_event_count = 0
        self.duplicate_adaptation_event_count = 0
        self.last_adaptation_event: dict[str, Any] | None = None
        self._processed_adaptation_event_ids: set[str] = set()
        self.runtime_revision = 0
        self.runtime_configuration_current: dict[str, Any] | None = None
        self.runtime_events: dict[str, dict[str, Any]] = {}
        self.runtime_last_result: dict[str, Any] | None = None
        self.runtime_history_error: str | None = None
        self.recovery_status = "disabled"
        self.recovery_error: str | None = None
        self.state_path = (
            Path(self.settings.experiment_root).expanduser().resolve(strict=False)
            / CONTROLLER_STATE_FILENAME
            if self.settings.experiment_root
            else None
        )
        if self.state_path is not None:
            self._restore_persisted_state()
        self.runtime_history = RuntimeHistory(self.state_path.parent / ".controller_runtime_history.json") if self.state_path else None
        for entry in self.runtime_events.values():
            self._archive_runtime_event(entry["command"], entry["result"])

    def _archive_runtime_event(self, command, result):
        if self.runtime_history is None:
            return
        try:
            self.runtime_history.save({"command": command, "status": result["status"],
                                       "result": result, "persistence": self.recovery_status})
            self.runtime_history_error = None
        except (OSError, ValueError) as exc:
            self.runtime_history_error = f"Runtime history persistence failed: {type(exc).__name__}"

    def runtime_history_page(self, run_id: str, before: int | None = None,
                             after: int | None = None, limit: int = 50):
        with self._lock:
            # Replay in-memory outcomes after a temporary journal failure.
            for entry in self.runtime_events.values():
                self._archive_runtime_event(entry["command"], entry["result"])
            page = self.runtime_history.page(run_id, before=before, after=after, limit=limit) if self.runtime_history else {"entries": [], "next_cursor": None}
            return {**page, "error": self.runtime_history_error,
                    "persistence_enabled": self.runtime_history is not None}

    def _build_process_adapter(self) -> ProcessAdapter:
        if not self.settings.opcua_enabled:
            return NoOpProcessAdapter()
        if not self.settings.opcua_endpoint:
            raise ValueError(
                "CONTROLLER_OPCUA_ENDPOINT is required when OPC UA is enabled."
            )
        return OpcUaProcessAdapter(
            OpcUaProcessConfig(
                endpoint=self.settings.opcua_endpoint,
                nodes=OpcUaNodeConfig(
                    namespace_index=self.settings.opcua_namespace_index,
                    T_j_set=self.settings.opcua_t_j_set_node,
                    T=self.settings.opcua_t_node,
                    T_j=self.settings.opcua_t_j_node,
                    c=self.settings.opcua_c_node,
                    count_middle=self.settings.opcua_count_middle_node,
                ),
                timeout_s=self.settings.opcua_timeout_s,
                jacket_min_K=self.settings.opcua_jacket_min_K,
                jacket_max_K=self.settings.opcua_jacket_max_K,
                verify_write=self.settings.opcua_verify_write,
                verify_tolerance_K=self.settings.opcua_verify_tolerance_K,
            )
        )

    def _state_document_locked(self) -> dict[str, Any]:
        adapter_state = self.adapter.export_state()
        if adapter_state is not None and not isinstance(adapter_state, Mapping):
            raise ValueError("Controller adapter export_state() must return an object or None.")
        return {
            "schema_version": 1,
            "updated_at": utc_ts(),
            "state": self.state.value,
            "parameters": copy.deepcopy(self.parameters),
            "parameter_version": self.parameter_version,
            "runtime_controls": {
                "revision": self.runtime_revision,
                "configuration": copy.deepcopy(self.runtime_configuration_current),
                "events": copy.deepcopy(self.runtime_events),
                "last_result": copy.deepcopy(self.runtime_last_result),
            },
            "current_run_id": self.current_run_id,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_frame_seq": self.last_frame_seq,
            "last_sample": copy.deepcopy(self.last_sample),
            "last_valid_sample": (
                self.last_valid_sample.to_dict()
                if self.last_valid_sample is not None
                else None
            ),
            "gsensor": {
                "desired_enabled": self.gsensor_desired_enabled,
                "enabled": self.gsensor_enabled,
                "measurement_ready": self.gsensor_measurement_ready,
                "control_revision": self.gsensor_control_revision,
                "control_event_id": self.gsensor_control_event_id,
                "last_command_enabled": self.gsensor_last_command_enabled,
                "last_command_requested_at": self.gsensor_last_command_requested_at,
                "initialization_generation": self.gsensor_initialization_generation,
                "alignment_revision": self.gsensor_alignment_revision,
                "sample_floor_frame_seq": self.gsensor_sample_floor_frame_seq,
                "transition_occurred_at": self.gsensor_transition_occurred_at,
                "last_status_at": self.gsensor_last_status_at,
                "gate_reason": self.gsensor_gate_reason,
            },
            "last_message": copy.deepcopy(self.last_message),
            "last_message_result": copy.deepcopy(self.last_message_result),
            "last_error": self.last_error,
            "counts": {
                "received": self.received_message_count,
                "accepted": self.accepted_message_count,
                "rejected": self.rejected_message_count,
                "valid": self.valid_sample_count,
                "invalid": self.invalid_sample_count,
                "duplicate": self.duplicate_sample_count,
                "missing": self.missing_frame_count,
                "adapter_error": self.adapter_error_count,
                "process_error": self.process_error_count,
                "control_tick": self.control_tick_count,
                "control_tick_error": self.control_tick_error_count,
            },
            "control_output": {
                "count": self.control_output_count,
                "invalid_count": self.invalid_control_output_count,
                "last": copy.deepcopy(self.last_control_output),
                "influx_write_success": self.influx_write_success_count,
                "influx_write_failure": self.influx_write_failure_count,
                "last_influx_write_at": self.last_influx_write_at,
                "last_influx_error": self.last_influx_error,
            },
            "seed_events": {
                "count": self.seed_event_count,
                "duplicate_count": self.duplicate_seed_event_count,
                "last": copy.deepcopy(self.last_seed_event),
                "processed_event_ids": sorted(self._processed_seed_event_ids),
            },
            "adaptation": {
                "enabled": self.adaptation_enabled,
                "mode": self.adaptation_mode,
                "last_success_at": self.last_fit_success_at,
                "event_count": self.adaptation_event_count,
                "duplicate_count": self.duplicate_adaptation_event_count,
                "last_event": copy.deepcopy(self.last_adaptation_event),
                "processed_event_ids": sorted(
                    self._processed_adaptation_event_ids
                ),
            },
            "adapter": {
                "class": (
                    f"{type(self.adapter).__module__}."
                    f"{type(self.adapter).__qualname__}"
                ),
                "state": copy.deepcopy(dict(adapter_state))
                if adapter_state is not None
                else None,
            },
            "process_io": {
                "last_error": self.last_process_error,
                "last_state": copy.deepcopy(self.last_process_state),
                "last_write": copy.deepcopy(self.last_process_write),
            },
            "scheduler": {
                "last_growth_sample_age_s": self.last_growth_sample_age_s,
            },
        }

    def _try_persist_state_locked(self) -> None:
        # An unreadable/incompatible archive is evidence, not a fresh state file.
        # In particular, shutdown and rejected messages must not overwrite it.
        if self.state_path is None or self.recovery_status == "error":
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            document = self._state_document_locked()
            temporary = self.state_path.with_name(
                f".{self.state_path.name}.{uuid4().hex}.tmp"
            )
            try:
                serialized = json.dumps(
                        document,
                        indent=2,
                        ensure_ascii=False,
                        allow_nan=False,
                    ) + "\n"
                with temporary.open("w", encoding="utf-8") as stream:
                    stream.write(serialized)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.state_path)
                if self.recovery_status == "persistence_error":
                    self.recovery_status = "saved"
                    self.recovery_error = None
            finally:
                if temporary.exists():
                    temporary.unlink()
        except Exception as exc:
            self.recovery_status = "persistence_error"
            self.recovery_error = str(exc)
            logger.exception("Could not persist Controller runtime state.")

    def _restore_persisted_state(self) -> None:
        assert self.state_path is not None
        if not self.state_path.is_file():
            self.recovery_status = "not_available"
            return
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("Controller runtime state must be an object.")
            if int(document.get("schema_version", 0)) != 1:
                raise ValueError("Unsupported Controller runtime-state schema.")
            state = ControllerState(str(document.get("state") or ""))
            parameters = document.get("parameters")
            if not isinstance(parameters, dict):
                raise ValueError("Controller recovery parameters must be an object.")
            counts = document.get("counts") or {}
            if not isinstance(counts, dict):
                raise ValueError("Controller recovery counts must be an object.")
            seed_events = document.get("seed_events") or {}
            if not isinstance(seed_events, dict):
                raise ValueError("Controller recovery seed events must be an object.")
            adaptation = document.get("adaptation") or {}
            if not isinstance(adaptation, dict):
                raise ValueError("Controller recovery adaptation must be an object.")
            self.last_fit_success_at = adaptation.get("last_success_at")
            control_output = document.get("control_output") or {}
            if not isinstance(control_output, dict):
                raise ValueError("Controller recovery control output must be an object.")

            self.state = state
            self.parameters = copy.deepcopy(parameters)
            parameter_version = document.get("parameter_version")
            self.parameter_version = (
                int(parameter_version) if parameter_version is not None else None
            )
            self.current_run_id = document.get("current_run_id")
            self.started_at = document.get("started_at")
            self.stopped_at = document.get("stopped_at")
            last_frame_seq = document.get("last_frame_seq")
            self.last_frame_seq = (
                int(last_frame_seq) if last_frame_seq is not None else None
            )
            self.last_sample = copy.deepcopy(document.get("last_sample"))
            last_valid_sample = document.get("last_valid_sample")
            self.last_valid_sample = (
                GrowthRateSamplePayload.from_mapping(last_valid_sample)
                if isinstance(last_valid_sample, Mapping)
                else None
            )
            gsensor = document.get("gsensor") or {}
            if not isinstance(gsensor, Mapping):
                raise ValueError("Controller recovery GSensor state must be an object.")
            control_revision = gsensor.get("control_revision", 0)
            initialization_generation = gsensor.get("initialization_generation", 0)
            alignment_revision = gsensor.get("alignment_revision", 0)
            sample_floor = gsensor.get("sample_floor_frame_seq", 0)
            for name, value in (
                ("control_revision", control_revision),
                ("initialization_generation", initialization_generation),
                ("alignment_revision", alignment_revision),
                ("sample_floor_frame_seq", sample_floor),
            ):
                if type(value) is not int or value < 0:
                    raise ValueError(f"Controller recovery GSensor {name} is invalid.")
            control_event_id = gsensor.get("control_event_id")
            if control_event_id is not None and (
                not isinstance(control_event_id, str) or not control_event_id.strip()
            ):
                raise ValueError("Controller recovery GSensor event ID is invalid.")
            last_command_enabled = gsensor.get("last_command_enabled")
            if last_command_enabled is not None and type(last_command_enabled) is not bool:
                raise ValueError("Controller recovery GSensor command state is invalid.")
            for name in (
                "last_command_requested_at",
                "transition_occurred_at",
                "last_status_at",
            ):
                timestamp = gsensor.get(name)
                if timestamp is not None:
                    _timestamp_value(timestamp, f"gsensor.{name}")
            self.gsensor_control_revision = control_revision
            self.gsensor_control_event_id = control_event_id
            self.gsensor_last_command_enabled = last_command_enabled
            self.gsensor_last_command_requested_at = gsensor.get(
                "last_command_requested_at"
            )
            self.gsensor_initialization_generation = initialization_generation
            self.gsensor_alignment_revision = alignment_revision
            self.gsensor_sample_floor_frame_seq = max(
                sample_floor,
                self.last_frame_seq or 0,
            )
            self.gsensor_transition_occurred_at = gsensor.get(
                "transition_occurred_at"
            )
            self.gsensor_last_status_at = gsensor.get("last_status_at")
            desired_enabled = gsensor.get("desired_enabled", False)
            if type(desired_enabled) is not bool:
                raise ValueError("Controller recovery GSensor desired state is invalid.")
            # Permission can survive a Controller-only restart while the GSensor
            # process keeps running. Availability and sample freshness cannot.
            self.gsensor_desired_enabled = desired_enabled
            self.gsensor_enabled = False
            self.gsensor_measurement_ready = False
            self.gsensor_gate_reason = (
                "restart_waiting_for_fresh_status"
                if desired_enabled
                else "disabled"
            )
            restart_barrier = _utc_timestamp(self._wall_clock())
            prior_barrier = self.gsensor_transition_occurred_at
            if prior_barrier is None or _timestamp_value(
                restart_barrier,
                "restart barrier",
            ) >= _timestamp_value(prior_barrier, "saved GSensor transition occurred_at"):
                self.gsensor_transition_occurred_at = restart_barrier
            self.gsensor_last_status_at = None
            self.last_valid_sample = None
            self.last_valid_sample_received_monotonic = None
            self.last_valid_sample_arrival_age_s = None
            self.last_message = copy.deepcopy(document.get("last_message"))
            self.last_message_result = copy.deepcopy(
                document.get("last_message_result")
            )
            self.last_error = document.get("last_error")
            self.received_message_count = int(counts.get("received", 0))
            self.accepted_message_count = int(counts.get("accepted", 0))
            self.rejected_message_count = int(counts.get("rejected", 0))
            self.valid_sample_count = int(counts.get("valid", 0))
            self.invalid_sample_count = int(counts.get("invalid", 0))
            self.duplicate_sample_count = int(counts.get("duplicate", 0))
            self.missing_frame_count = int(counts.get("missing", 0))
            self.adapter_error_count = int(counts.get("adapter_error", 0))
            self.process_error_count = int(counts.get("process_error", 0))
            self.control_tick_count = int(counts.get("control_tick", 0))
            self.control_tick_error_count = int(counts.get("control_tick_error", 0))
            scheduler = document.get("scheduler") or {}
            if not isinstance(scheduler, dict):
                raise ValueError("Controller recovery scheduler must be an object.")
            saved_age = scheduler.get("last_growth_sample_age_s")
            self.last_growth_sample_age_s = (
                float(saved_age) if saved_age is not None else None
            )
            process_io = document.get("process_io") or {}
            if not isinstance(process_io, dict):
                raise ValueError("Controller recovery process_io must be an object.")
            self.last_process_error = process_io.get("last_error")
            self.last_process_state = copy.deepcopy(process_io.get("last_state"))
            self.last_process_write = copy.deepcopy(process_io.get("last_write"))
            self.control_output_count = int(control_output.get("count", 0))
            self.invalid_control_output_count = int(
                control_output.get("invalid_count", 0)
            )
            last_control_output = control_output.get("last")
            if last_control_output is not None:
                if not isinstance(last_control_output, dict):
                    raise ValueError(
                        "Controller last control output must be an object or null."
                    )
                result_document = last_control_output.get("result")
                if not isinstance(result_document, Mapping):
                    raise ValueError(
                        "Controller last control output is missing its result object."
                    )
                ControllerStepResult.from_mapping(result_document)
            self.last_control_output = copy.deepcopy(last_control_output)
            self.influx_write_success_count = int(
                control_output.get("influx_write_success", 0)
            )
            self.influx_write_failure_count = int(
                control_output.get("influx_write_failure", 0)
            )
            self.last_influx_write_at = control_output.get("last_influx_write_at")
            persisted_influx_error = control_output.get("last_influx_error")
            if self.influx_initialization_error is None:
                self.last_influx_error = persisted_influx_error
            self.seed_event_count = int(seed_events.get("count", 0))
            self.duplicate_seed_event_count = int(
                seed_events.get("duplicate_count", 0)
            )
            last_seed_event = seed_events.get("last")
            if last_seed_event is not None and not isinstance(last_seed_event, dict):
                raise ValueError("Controller last seed event must be an object or null.")
            self.last_seed_event = copy.deepcopy(last_seed_event)
            processed_seed_event_ids = seed_events.get("processed_event_ids") or []
            if not isinstance(processed_seed_event_ids, list) or not all(
                isinstance(event_id, str) and event_id.strip()
                for event_id in processed_seed_event_ids
            ):
                raise ValueError(
                    "Controller processed seed event IDs must be a list of strings."
                )
            self._processed_seed_event_ids = set(processed_seed_event_ids)
            adaptation_enabled = adaptation.get("enabled", False)
            if not isinstance(adaptation_enabled, bool):
                raise ValueError(
                    "Controller recovery adaptation enabled must be a boolean."
                )
            self.adaptation_enabled = adaptation_enabled
            self.adaptation_mode = str(adaptation.get("mode", "E_A"))
            # Reuse the shared contract to validate both mode and strict bool.
            ControllerAdaptationPayload(
                run_id=str(self.current_run_id or "recovery"),
                event_id="recovery-validation",
                enabled=self.adaptation_enabled,
                mode=self.adaptation_mode,
                requested_at=str(document.get("updated_at") or "recovery"),
            )
            self.adaptation_event_count = int(adaptation.get("event_count", 0))
            self.duplicate_adaptation_event_count = int(
                adaptation.get("duplicate_count", 0)
            )
            last_adaptation_event = adaptation.get("last_event")
            if last_adaptation_event is not None and not isinstance(
                last_adaptation_event, dict
            ):
                raise ValueError(
                    "Controller last adaptation event must be an object or null."
                )
            self.last_adaptation_event = copy.deepcopy(last_adaptation_event)
            processed_adaptation_event_ids = (
                adaptation.get("processed_event_ids") or []
            )
            if not isinstance(processed_adaptation_event_ids, list) or not all(
                isinstance(event_id, str) and event_id.strip()
                for event_id in processed_adaptation_event_ids
            ):
                raise ValueError(
                    "Controller processed adaptation event IDs must be a list of strings."
                )
            self._processed_adaptation_event_ids = set(
                processed_adaptation_event_ids
            )

            if state == ControllerState.RUNNING:
                if not self.current_run_id or self.parameter_version is None:
                    raise ValueError("Running Controller recovery state is incomplete.")
                adapter_document = document.get("adapter")
                adapter_state = (
                    adapter_document.get("state")
                    if isinstance(adapter_document, dict)
                    else None
                )
                if not isinstance(adapter_state, dict):
                    raise ValueError(
                        "The configured Controller adapter does not provide restart state."
                    )
                restored = self.adapter.restore_state(
                    copy.deepcopy(self.parameters),
                    str(self.current_run_id),
                    copy.deepcopy(adapter_state),
                )
                if not restored:
                    raise ValueError(
                        "The configured Controller adapter cannot restore a running experiment. "
                        "Its checkpoint content is incomplete, invalid, or incompatible. "
                        "Preserve the checkpoint and create a new experiment in a new session "
                        "directory; do not substitute the latest process temperature."
                    )
                self.adapter.set_adaptation(
                    self.adaptation_enabled,
                    self.adaptation_mode,
                    copy.deepcopy(self.last_adaptation_event),
                )
                if self.settings.opcua_enabled:
                    self.process_adapter.connect()
                now = self._monotonic_clock()
                self._run_started_monotonic = now - self.control_tick_count * float(
                    self.parameters.get("dt", 5.0)
                )
                self._next_tick_monotonic = now + float(
                    self.parameters.get("dt", 5.0)
                )
            runtime = document.get("runtime_controls")
            actual_configuration = self.adapter.runtime_configuration() if state == ControllerState.RUNNING else None
            if runtime is not None:
                if not isinstance(runtime, dict):
                    raise ValueError("Invalid runtime controls recovery document.")
                revision = runtime.get("revision")
                if type(revision) is not int or revision < 0:
                    raise ValueError("Invalid runtime controls revision.")
                saved_configuration = runtime.get("configuration")
                if saved_configuration is not None:
                    saved_configuration = validate_runtime_changes(saved_configuration)
                    if state == ControllerState.RUNNING and saved_configuration != actual_configuration:
                        raise ValueError("Runtime controls disagree with recovered algorithm state.")
                events = runtime.get("events")
                if not isinstance(events, dict):
                    raise ValueError("Invalid runtime event journal.")
                for event_id, entry in events.items():
                    event = ControllerRuntimeUpdatePayload.from_mapping(entry["command"])
                    if event_id != event.event_id or event.run_id != self.current_run_id:
                        raise ValueError("Runtime event journal belongs to another run.")
                    if not isinstance(entry.get("result"), dict):
                        raise ValueError("Runtime event result is missing.")
                self.runtime_revision = revision
                self.runtime_configuration_current = copy.deepcopy(saved_configuration)
                self.runtime_events = copy.deepcopy(events)
                self.runtime_last_result = copy.deepcopy(runtime.get("last_result"))
            else:
                # Old archives already contain the algorithm's actual parameters
                # and adaptation settings. No migration of GSensor state is needed.
                self.runtime_configuration_current = (
                    copy.deepcopy(dict(actual_configuration))
                    if actual_configuration is not None else None
                )
            self.recovery_status = "restored"
            self.recovery_error = None
        except Exception as exc:
            self.state = ControllerState.ERROR
            self._next_tick_monotonic = None
            try:
                self.adapter.stop()
            except Exception:
                logger.exception("Could not stop adapter after failed recovery.")
            try:
                self.process_adapter.disconnect()
            except Exception:
                logger.exception("Could not disconnect process after failed recovery.")
            self.last_error = f"Controller recovery failed: {exc}"
            self.recovery_status = "error"
            self.recovery_error = str(exc)
            logger.exception("Could not restore Controller runtime state.")

    def start(self) -> None:
        if not self._control_thread or not self._control_thread.is_alive():
            self._control_stop.clear()
            self._control_thread = threading.Thread(
                target=self._control_forever,
                name="controller-control-clock",
                daemon=True,
            )
            self._control_thread.start()
        with self._lock:
            if self._consumer_thread and self._consumer_thread.is_alive():
                return
            self._consumer_stop.clear()
            self.consumer_status = "connecting"
            self._consumer_thread = threading.Thread(
                target=self._consume_forever,
                name="controller-rabbitmq-consumer",
                daemon=True,
            )
            self._consumer_thread.start()

    def stop(self) -> None:
        self._consumer_stop.set()
        self._control_stop.set()
        self._control_wakeup.set()
        with self._lock:
            if self.state == ControllerState.RUNNING:
                # Capture the live adapter before asking it to release resources.
                self._try_persist_state_locked()
                try:
                    self.adapter.stop()
                except Exception:
                    self.adapter_error_count += 1
                    logger.exception("Controller adapter failed during shutdown.")
            try:
                self.process_adapter.disconnect()
            except Exception:
                self.process_error_count += 1
                logger.exception("Controller process adapter failed during shutdown.")
            self.consumer_status = "stopped"
            # A process shutdown is not an experiment.stop. Preserve the
            # logical RUNNING state so a replacement container can recover it.
            if self.state != ControllerState.RUNNING:
                self._try_persist_state_locked()
        if not self._measurement_writer_closed:
            close_writer = getattr(self.measurement_writer, "close", None)
            if callable(close_writer):
                try:
                    close_writer()
                except Exception:
                    logger.exception("Controller InfluxDB writer failed during shutdown.")
            self._measurement_writer_closed = True

    def _control_forever(self) -> None:
        """Run control ticks from a monotonic clock, never from G frame arrival."""

        while not self._control_stop.is_set():
            with self._lock:
                deadline = (
                    self._next_tick_monotonic
                    if self.state == ControllerState.RUNNING
                    else None
                )
            if deadline is None:
                self._control_wakeup.wait(0.25)
                self._control_wakeup.clear()
                continue
            delay = max(0.0, deadline - self._monotonic_clock())
            if self._control_wakeup.wait(delay):
                self._control_wakeup.clear()
                continue
            try:
                self._control_tick_once(now=self._monotonic_clock())
            except Exception:
                logger.exception("Controller control tick failed.")

    def _consume_forever(self) -> None:
        while not self._consumer_stop.is_set():
            try:
                self._consumer_runner(
                    url=self.settings.rabbit_url,
                    exchange=self.settings.rabbit_exchange,
                    queue_name=self.settings.rabbit_queue,
                    binding_keys=bindings_for(ROLE),
                    on_message=self.on_message,
                    on_ready=self._consumer_ready,
                )
            except Exception as exc:
                if self._consumer_stop.is_set():
                    break
                with self._lock:
                    self.consumer_status = "reconnecting"
                    self.last_error = f"RabbitMQ consumer error: {exc}"
                logger.exception("Controller RabbitMQ consumer stopped; retrying.")
                self._consumer_stop.wait(5)

    def _consumer_ready(self) -> None:
        with self._lock:
            self.consumer_status = "consuming"

    def on_message(self, message: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.received_message_count += 1
            self.last_message = copy.deepcopy(message)

        try:
            result = self._dispatch(message)
        except Exception as exc:
            result = {"accepted": False, "reason": str(exc)}
            with self._lock:
                self.rejected_message_count += 1
                self.last_error = str(exc)
            logger.warning("Controller rejected message: %s", exc)
        else:
            with self._lock:
                if result.get("accepted"):
                    self.accepted_message_count += 1
                    self.last_error = None
                else:
                    self.rejected_message_count += 1
                    self.last_error = result.get("reason")

        with self._lock:
            self.last_message_result = copy.deepcopy(result)
            self._try_persist_state_locked()
            if result.get("kind") == "runtime" and result.get("event_id") in self.runtime_events:
                event = self.runtime_events[result["event_id"]]
                self._archive_runtime_event(event["command"], event["result"])
        return result

    def _dispatch(self, message: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(message, Mapping):
            raise ValueError("Message envelope must be an object.")
        if message.get("dst") != ROLE:
            raise ValueError("Message dst must be 'controller'.")

        msg_type = str(message.get("msg_type", ""))
        name = str(message.get("name", ""))
        source = str(message.get("src", ""))
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("Message payload must be an object.")

        if msg_type == "params" and name == PARAMS_UPDATE_MESSAGE:
            if source != "central":
                raise ValueError("params.update must come from Central.")
            return self._apply_parameters(payload)

        if msg_type == "command" and name == EXPERIMENT_START_COMMAND:
            if source != "central":
                raise ValueError("experiment.start must come from Central.")
            return self._start_experiment(payload)

        if msg_type == "command" and name == EXPERIMENT_STOP_COMMAND:
            if source != "central":
                raise ValueError("experiment.stop must come from Central.")
            return self._stop_experiment(payload)

        if msg_type == "command" and name == CONTROLLER_ADD_SEED_COMMAND:
            if source != "central":
                raise ValueError("controller.add_seed must come from Central.")
            return self._add_seed(payload)

        if msg_type == "command" and name == CONTROLLER_ADAPTATION_SET_COMMAND:
            if source != "central":
                raise ValueError("controller.adaptation.set must come from Central.")
            return self._set_adaptation(payload)

        if msg_type == "command" and name == CONTROLLER_RUNTIME_UPDATE_COMMAND:
            if source != "central":
                raise ValueError("controller.runtime.update must come from Central.")
            return self._update_runtime(payload)

        if msg_type == "command" and name in {
            GSENSOR_ENABLE_COMMAND,
            GSENSOR_DISABLE_COMMAND,
        }:
            if source != "central":
                raise ValueError(f"{name} must come from Central.")
            return self._set_gsensor_enabled(
                payload,
                enabled=name == GSENSOR_ENABLE_COMMAND,
            )

        if msg_type == "status" and name == GROWTH_RATE_STATUS_MESSAGE:
            if source != "gsensor":
                raise ValueError("growth_rate.status must come from Gsensor.")
            return self._accept_growth_status(payload)

        if msg_type == "measurement" and name == GROWTH_RATE_SAMPLE_MESSAGE:
            if source != "gsensor":
                raise ValueError("growth_rate.sample must come from Gsensor.")
            return self._accept_sample(payload)

        raise ValueError(f"Unsupported Controller message: {msg_type}/{name}.")

    def _apply_parameters(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        version = _positive_int(payload.get("version"), "version")
        params = payload.get("params")
        if not isinstance(params, Mapping):
            raise ValueError("params.update payload.params must be an object.")
        normalized = {str(key): copy.deepcopy(value) for key, value in params.items()}

        with self._lock:
            if self.state == ControllerState.RUNNING:
                if version == self.parameter_version and normalized == self.parameters:
                    return {"accepted": True, "duplicate": True, "kind": "params"}
                raise ValueError("Controller parameters cannot change during an experiment.")
            self.parameters = normalized
            self.parameter_version = version
            self.state = ControllerState.CONFIGURED

        return {
            "accepted": True,
            "kind": "params",
            "parameter_version": version,
            "parameter_count": len(normalized),
        }

    def _start_experiment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _positive_int(payload.get("parameter_version"), "parameter_version")
        command = ExperimentStartPayload.from_mapping(payload)
        with self._lock:
            if self.recovery_status == "error":
                raise ValueError(
                    "Preserve the failed checkpoint and create a new experiment in a new "
                    "session directory before restarting Controller."
                )
            if self.parameter_version is None:
                raise ValueError("Controller has not received params.update.")
            if command.parameter_version != self.parameter_version:
                raise ValueError(
                    "experiment.start parameter_version does not match Controller params."
                )
            if self.current_run_id == command.run_id:
                if self.state == ControllerState.RUNNING:
                    return {"accepted": True, "duplicate": True, "kind": "start"}
                if self.state == ControllerState.STOPPED:
                    raise ValueError("A stopped experiment cannot be restarted.")
            if self.current_run_id and self.state == ControllerState.RUNNING:
                raise ValueError("Controller is already running another experiment.")
            run_type = str(
                self.parameters.get("run_type", self.parameters.get("exp_sim", "experiment"))
            )
            if run_type == "experiment" and not self.settings.opcua_enabled:
                raise ValueError(
                    "Experiment mode requires CONTROLLER_OPCUA_ENABLED=true for process reads."
                )

            self.current_run_id = command.run_id
            self.started_at = command.started_at
            self.stopped_at = None
            self.last_frame_seq = None
            self.last_sample = None
            self.last_valid_sample = None
            self.last_valid_sample_received_monotonic = None
            self.last_valid_sample_arrival_age_s = None
            self.gsensor_desired_enabled = False
            self.gsensor_enabled = False
            self.gsensor_measurement_ready = False
            self.gsensor_control_revision = 0
            self.gsensor_control_event_id = None
            self.gsensor_last_command_enabled = None
            self.gsensor_last_command_requested_at = None
            self.gsensor_initialization_generation = 0
            self.gsensor_alignment_revision = 0
            self.gsensor_sample_floor_frame_seq = 0
            self.gsensor_transition_occurred_at = command.started_at
            self.gsensor_last_status_at = None
            self.gsensor_gate_reason = "disabled"
            self.control_tick_count = 0
            self.control_tick_error_count = 0
            self.last_growth_sample_age_s = None
            self.valid_sample_count = 0
            self.invalid_sample_count = 0
            self.duplicate_sample_count = 0
            self.missing_frame_count = 0
            self.last_control_output = None
            self.control_output_count = 0
            self.invalid_control_output_count = 0
            self.process_error_count = 0
            self.last_process_error = None
            self.last_process_state = None
            self.last_process_write = None
            self.influx_write_success_count = 0
            self.influx_write_failure_count = 0
            self.last_influx_write_at = None
            self.last_influx_error = self.influx_initialization_error
            self.seed_event_count = 0
            self.duplicate_seed_event_count = 0
            self.last_seed_event = None
            self._processed_seed_event_ids.clear()
            self.adaptation_enabled = command.adaptation_enabled
            self.adaptation_mode = command.adaptation_mode
            self.last_fit_success_at = None
            self.adaptation_event_count = 0
            self.duplicate_adaptation_event_count = 0
            self.last_adaptation_event = None
            self._processed_adaptation_event_ids.clear()
            self.runtime_revision = 0
            self.runtime_events.clear()
            self.runtime_last_result = None
            self.runtime_configuration_current = None

            try:
                self.adapter.configure(copy.deepcopy(self.parameters), command.run_id)
                self.adapter.set_adaptation(
                    self.adaptation_enabled,
                    self.adaptation_mode,
                    None,
                )
            except Exception as exc:
                self.adapter_error_count += 1
                self.state = ControllerState.ERROR
                raise RuntimeError(f"Controller adapter configure failed: {exc}") from exc
            activation_state = None
            if self.settings.opcua_enabled:
                try:
                    self.process_adapter.connect()
                    if run_type == "experiment":
                        activation_state = self.process_adapter.read_state()
                        if not isinstance(activation_state, ProcessState):
                            raise TypeError("Process activation requires ProcessState.")
                        activation_state.__post_init__()
                except Exception as exc:
                    self.process_error_count += 1
                    self.last_process_error = str(exc)
                    self.process_adapter.disconnect()
                    self.state = ControllerState.ERROR
                    raise RuntimeError(
                        f"Controller process activation failed: {exc}"
                    ) from exc
            try:
                self.adapter.start()
                if activation_state is not None:
                    self.adapter.initialize_process_state(activation_state)
            except Exception as exc:
                self.adapter_error_count += 1
                try:
                    self.adapter.stop()
                except Exception:
                    logger.exception("Could not stop adapter after failed activation.")
                self.process_adapter.disconnect()
                self.state = ControllerState.ERROR
                raise RuntimeError(f"Controller adapter start failed: {exc}") from exc
            self.state = ControllerState.RUNNING
            configuration = self.adapter.runtime_configuration()
            self.runtime_configuration_current = (
                copy.deepcopy(dict(configuration)) if configuration is not None else None
            )
            now = self._monotonic_clock()
            self._run_started_monotonic = now
            self._next_tick_monotonic = now + float(self.parameters.get("dt", 5.0))
            self._control_wakeup.set()

        return {"accepted": True, "kind": "start", "run_id": command.run_id}

    def _stop_experiment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        command = ExperimentStopPayload.from_mapping(payload)
        with self._lock:
            if self.current_run_id is None:
                raise ValueError("Controller has no current experiment.")
            if command.run_id != self.current_run_id:
                raise ValueError("experiment.stop run_id does not match current experiment.")
            if self.state == ControllerState.STOPPED:
                return {"accepted": True, "duplicate": True, "kind": "stop"}
            try:
                self.adapter.stop()
            except Exception as exc:
                self.adapter_error_count += 1
                self.state = ControllerState.ERROR
                raise RuntimeError(f"Controller adapter stop failed: {exc}") from exc
            finally:
                self.process_adapter.disconnect()
            self.state = ControllerState.STOPPED
            self.stopped_at = command.stopped_at
            self._invalidate_growth_input_locked(
                reason="experiment_stopped",
                frame_seq=self.last_frame_seq,
                occurred_at=command.stopped_at,
            )
            self.gsensor_desired_enabled = False
            self.gsensor_enabled = False
            self._next_tick_monotonic = None
            self._control_wakeup.set()

        return {"accepted": True, "kind": "stop", "run_id": command.run_id}

    def _add_seed(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        event = ControllerAddSeedPayload.from_mapping(payload)
        with self._lock:
            if self.current_run_id is None:
                raise ValueError("Controller has no current experiment.")
            if event.run_id != self.current_run_id:
                raise ValueError(
                    "controller.add_seed run_id does not match current experiment."
                )
            if event.event_id in self._processed_seed_event_ids:
                self.duplicate_seed_event_count += 1
                return {
                    "accepted": True,
                    "duplicate": True,
                    "kind": "seed_event",
                    "event_id": event.event_id,
                }
            if self.state != ControllerState.RUNNING:
                raise ValueError("Controller is not running an experiment.")

            event_document = event.to_dict()
            try:
                self.adapter.add_seed(copy.deepcopy(event_document))
            except Exception as exc:
                self.adapter_error_count += 1
                self.state = ControllerState.ERROR
                raise RuntimeError(
                    f"Controller adapter Add Seed failed: {exc}"
                ) from exc

            self._processed_seed_event_ids.add(event.event_id)
            self.seed_event_count += 1
            self.last_seed_event = event_document

        return {
            "accepted": True,
            "kind": "seed_event",
            "event_id": event.event_id,
            "adapter_called": True,
        }

    def _set_adaptation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        event = ControllerAdaptationPayload.from_mapping(payload)
        with self._lock:
            if self.current_run_id is None:
                raise ValueError("Controller has no current experiment.")
            if event.run_id != self.current_run_id:
                raise ValueError(
                    "controller.adaptation.set run_id does not match current experiment."
                )
            if (event.event_id in self._processed_adaptation_event_ids
                    and event.event_id not in self.runtime_events):
                self.duplicate_adaptation_event_count += 1
                return {
                    "accepted": True,
                    "duplicate": True,
                    "kind": "adaptation",
                    "event_id": event.event_id,
                }
            if self.state != ControllerState.RUNNING:
                raise ValueError("Controller is not running an experiment.")

            # Compatibility transport only: same atomic path and unsupported-
            # adapter rejection as the new runtime command.
            prior = self.runtime_events.get(event.event_id)
            revision = (
                prior["command"]["expected_revision"] if prior else self.runtime_revision
            )
            result = self._update_runtime(ControllerRuntimeUpdatePayload(
                event.run_id, event.event_id, revision,
                {"adaptation_enabled": event.enabled, "adaptation_mode": event.mode},
                event.requested_at,
            ).to_dict())
            if result.get("duplicate"):
                self.duplicate_adaptation_event_count += 1
            return result

    def _update_runtime(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        event = ControllerRuntimeUpdatePayload.from_mapping(payload)
        with self._lock:
            if event.run_id != self.current_run_id:
                raise ValueError("Runtime run_id does not match current experiment.")
            command = event.to_dict()
            prior = self.runtime_events.get(event.event_id)
            if prior:
                if prior["command"] != command:
                    raise ValueError("Runtime event_id was reused with different content.")
                result = {**copy.deepcopy(prior["result"]), "duplicate": True}
                self.runtime_last_result = result
                return result
            before = copy.deepcopy(self.runtime_configuration_current)
            result = {
                "kind": "runtime", "event_id": event.event_id, "run_id": event.run_id,
                "requested_at": event.requested_at, "processed_at": utc_ts(),
                "revision": self.runtime_revision, "before": before,
            }
            try:
                if self.state != ControllerState.RUNNING:
                    raise ValueError("Controller is not running an experiment.")
                if event.expected_revision != self.runtime_revision:
                    raise ValueError("Runtime configuration revision conflict.")
                if before is None:
                    raise ValueError("This Controller adapter does not support runtime updates.")
                after = dict(self.adapter.update_runtime(event.changes))
            except (ValueError, TypeError, NotImplementedError) as exc:
                result.update(accepted=False, status="rejected", reason=str(exc),
                              configuration=before)
            else:
                self.runtime_revision += 1
                self.runtime_configuration_current = copy.deepcopy(after)
                self.adaptation_enabled = after["adaptation_enabled"]
                self.adaptation_mode = after["adaptation_mode"]
                if {"adaptation_enabled", "adaptation_mode"} & event.changes.keys():
                    self.adaptation_event_count += 1
                    self._processed_adaptation_event_ids.add(event.event_id)
                    self.last_adaptation_event = {
                        "run_id": event.run_id, "event_id": event.event_id,
                        "enabled": self.adaptation_enabled, "mode": self.adaptation_mode,
                        "requested_at": event.requested_at,
                    }
                result.update(accepted=True, status="applied", revision=self.runtime_revision,
                              configuration=after, effective_tick=self.control_tick_count + 1,
                              no_change=before == after, applied_at=utc_ts())
            self.runtime_events[event.event_id] = {
                "command": command, "result": copy.deepcopy(result),
            }
            self.runtime_last_result = copy.deepcopy(result)
            return result

    def _invalidate_growth_input_locked(
        self,
        *,
        reason: str,
        frame_seq: int | None = None,
        occurred_at: str | None = None,
    ) -> None:
        """Close the live-G gate and move both replay barriers forward."""

        self.last_valid_sample = None
        self.last_valid_sample_received_monotonic = None
        self.last_valid_sample_arrival_age_s = None
        self.last_growth_sample_age_s = None
        self.gsensor_measurement_ready = False
        self.gsensor_gate_reason = reason
        if frame_seq is not None:
            self.gsensor_sample_floor_frame_seq = max(
                self.gsensor_sample_floor_frame_seq,
                int(frame_seq),
            )
        if occurred_at is not None:
            candidate = _timestamp_value(occurred_at, "GSensor transition occurred_at")
            current = (
                _timestamp_value(
                    self.gsensor_transition_occurred_at,
                    "saved GSensor transition occurred_at",
                )
                if self.gsensor_transition_occurred_at is not None
                else None
            )
            if current is None or candidate >= current:
                self.gsensor_transition_occurred_at = occurred_at

    def _set_gsensor_enabled(
        self,
        payload: Mapping[str, Any],
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        command = GsensorActivationPayload.from_mapping(payload)
        _timestamp_value(command.requested_at, "requested_at")
        with self._lock:
            if self.state != ControllerState.RUNNING:
                raise ValueError("Controller is not running an experiment.")
            if command.run_id != self.current_run_id:
                raise ValueError("GSensor activation run_id does not match current experiment.")
            if command.experiment_started_at != self.started_at:
                raise ValueError(
                    "GSensor activation experiment_started_at does not match current experiment."
                )
            if command.revision < self.gsensor_control_revision:
                raise ValueError("GSensor activation revision is stale.")
            if command.revision == self.gsensor_control_revision:
                if (
                    command.event_id == self.gsensor_control_event_id
                    and enabled == self.gsensor_last_command_enabled
                ):
                    return {
                        "accepted": True,
                        "duplicate": True,
                        "kind": "gsensor_activation",
                        "enabled": self.gsensor_enabled,
                        "requested_enabled": self.gsensor_desired_enabled,
                        "measurement_ready": self.gsensor_measurement_ready,
                        "control_revision": self.gsensor_control_revision,
                    }
                raise ValueError("GSensor activation revision conflicts with its prior event.")
            if command.event_id == self.gsensor_control_event_id:
                raise ValueError("A new GSensor activation revision requires a new event_id.")

            self.gsensor_control_revision = command.revision
            self.gsensor_control_event_id = command.event_id
            self.gsensor_last_command_enabled = enabled
            self.gsensor_last_command_requested_at = command.requested_at
            self.gsensor_desired_enabled = enabled
            # Central's request closes the gate immediately. Enabling still
            # requires a matching GSensor status acknowledgement.
            self.gsensor_enabled = False
            self._invalidate_growth_input_locked(
                reason="awaiting_gsensor_status" if enabled else "disabled",
                frame_seq=self.last_frame_seq,
                occurred_at=command.requested_at,
            )
            return {
                "accepted": True,
                "kind": "gsensor_activation",
                "enabled": False,
                "requested_enabled": enabled,
                "measurement_ready": False,
                "control_revision": command.revision,
                "control_event_id": command.event_id,
            }

    def _accept_growth_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        status = GrowthRateStatusPayload.from_mapping(payload)
        required = {
            "enabled": status.enabled,
            "control_revision": status.control_revision,
            "experiment_started_at": status.experiment_started_at,
            "initialization_generation": status.initialization_generation,
            "alignment_revision": status.alignment_revision,
            "measurement_ready": status.measurement_ready,
            "frame_seq": status.frame_seq,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "growth_rate.status is missing live identity field(s): "
                + ", ".join(missing)
            )
        status_time = _timestamp_value(status.occurred_at, "occurred_at")
        with self._lock:
            if self.state != ControllerState.RUNNING:
                raise ValueError("Controller is not running an experiment.")
            if status.run_id != self.current_run_id:
                raise ValueError("growth_rate.status run_id does not match current experiment.")
            if status.experiment_started_at != self.started_at:
                raise ValueError(
                    "growth_rate.status experiment_started_at does not match current experiment."
                )
            if status.control_revision != self.gsensor_control_revision:
                raise ValueError("growth_rate.status control revision is stale or unexpected.")
            if self.gsensor_control_revision > 0:
                if status.control_event_id != self.gsensor_control_event_id:
                    raise ValueError("growth_rate.status control event does not match current request.")
            elif status.control_event_id is not None:
                raise ValueError("growth_rate.status has an unexpected control event.")
            if status.enabled and not self.gsensor_desired_enabled:
                raise ValueError("growth_rate.status reports enabled without current authorization.")
            if status.measurement_ready and not status.enabled:
                raise ValueError("growth_rate.status cannot be ready while GSensor is disabled.")
            if self.gsensor_transition_occurred_at is not None and status_time < _timestamp_value(
                self.gsensor_transition_occurred_at,
                "saved GSensor transition occurred_at",
            ):
                raise ValueError("growth_rate.status predates the active transition barrier.")
            if self.gsensor_last_status_at is not None and status_time < _timestamp_value(
                self.gsensor_last_status_at,
                "saved GSensor status occurred_at",
            ):
                raise ValueError("growth_rate.status is older than the last accepted status.")
            assert status.initialization_generation is not None
            assert status.alignment_revision is not None
            if (
                status.initialization_generation < self.gsensor_initialization_generation
                or status.alignment_revision < self.gsensor_alignment_revision
            ):
                raise ValueError("growth_rate.status processing context regressed.")

            context_changed = (
                status.initialization_generation
                != self.gsensor_initialization_generation
                or status.alignment_revision != self.gsensor_alignment_revision
            )
            self.gsensor_initialization_generation = status.initialization_generation
            self.gsensor_alignment_revision = status.alignment_revision
            self.gsensor_last_status_at = status.occurred_at
            self.gsensor_enabled = bool(status.enabled)
            if context_changed or not status.enabled or not status.measurement_ready:
                reason = (
                    "processing_context_changed"
                    if context_changed
                    else "gsensor_not_enabled"
                    if not status.enabled
                    else "measurement_not_ready"
                )
                self._invalidate_growth_input_locked(
                    reason=reason,
                    frame_seq=status.frame_seq,
                    occurred_at=status.occurred_at,
                )
                if status.enabled and status.measurement_ready:
                    self.gsensor_measurement_ready = True
                    self.gsensor_gate_reason = "waiting_for_fresh_valid_sample"
            else:
                # Same-context ready notices are emitted before samples. They
                # acknowledge availability but must not move replay barriers.
                self.gsensor_measurement_ready = True
                self.gsensor_gate_reason = (
                    "ready"
                    if self.last_valid_sample is not None
                    else "waiting_for_fresh_valid_sample"
                )
            return {
                "accepted": True,
                "kind": "growth_status",
                "enabled": self.gsensor_enabled,
                "measurement_ready": self.gsensor_measurement_ready,
                "control_revision": self.gsensor_control_revision,
                "initialization_generation": self.gsensor_initialization_generation,
                "alignment_revision": self.gsensor_alignment_revision,
            }

    def _accept_sample(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _positive_int(payload.get("frame_seq"), "frame_seq")
        sample = GrowthRateSamplePayload.from_mapping(payload)
        processed_time = _timestamp_value(sample.processed_at, "processed_at")
        with self._lock:
            if self.state != ControllerState.RUNNING:
                raise ValueError("Controller is not running an experiment.")
            if sample.run_id != self.current_run_id:
                raise ValueError("growth_rate.sample run_id does not match current experiment.")
            identity = {
                "experiment_started_at": sample.experiment_started_at,
                "control_revision": sample.control_revision,
                "initialization_generation": sample.initialization_generation,
                "alignment_revision": sample.alignment_revision,
            }
            missing = [name for name, value in identity.items() if value is None]
            if missing:
                raise ValueError(
                    "growth_rate.sample is missing live identity field(s): "
                    + ", ".join(missing)
                )
            if sample.experiment_started_at != self.started_at:
                raise ValueError(
                    "growth_rate.sample experiment_started_at does not match current experiment."
                )
            if not self.gsensor_desired_enabled or not self.gsensor_enabled:
                raise ValueError("growth_rate.sample arrived while GSensor is disabled or unacknowledged.")
            if sample.control_revision != self.gsensor_control_revision:
                raise ValueError("growth_rate.sample control revision is stale.")
            if sample.initialization_generation != self.gsensor_initialization_generation:
                raise ValueError("growth_rate.sample initialization generation is stale.")
            if sample.alignment_revision != self.gsensor_alignment_revision:
                raise ValueError("growth_rate.sample alignment revision is stale.")
            diagnostic_at_transition = (
                not sample.valid
                and not self.gsensor_measurement_ready
                and sample.frame_seq == self.gsensor_sample_floor_frame_seq
            )
            if not self.gsensor_measurement_ready and not diagnostic_at_transition:
                raise ValueError("growth_rate.sample arrived before measurement became ready.")
            if (
                sample.frame_seq < self.gsensor_sample_floor_frame_seq
                or (
                    sample.frame_seq == self.gsensor_sample_floor_frame_seq
                    and not diagnostic_at_transition
                )
            ):
                raise ValueError("growth_rate.sample frame is behind the active transition floor.")
            if (
                not diagnostic_at_transition
                and self.gsensor_transition_occurred_at is not None
                and processed_time <= _timestamp_value(
                    self.gsensor_transition_occurred_at,
                    "saved GSensor transition occurred_at",
                )
            ):
                raise ValueError("growth_rate.sample predates the active transition barrier.")
            arrival_age = max(0.0, self._wall_clock() - processed_time)
            if arrival_age > 2.0 * float(self.parameters.get("dt_G", 15.0)):
                raise ValueError("growth_rate.sample was already stale when received.")
            if self.last_frame_seq is not None and sample.frame_seq <= self.last_frame_seq:
                self.duplicate_sample_count += 1
                return {
                    "accepted": False,
                    "duplicate": True,
                    "kind": "sample",
                    "frame_seq": sample.frame_seq,
                }
            if self.last_frame_seq is not None and sample.frame_seq > self.last_frame_seq + 1:
                self.missing_frame_count += sample.frame_seq - self.last_frame_seq - 1

            self.last_frame_seq = sample.frame_seq
            self.last_sample = sample.to_dict()
            if not sample.valid:
                self.invalid_sample_count += 1
                self.last_valid_sample = None
                self.last_valid_sample_received_monotonic = None
                self.last_valid_sample_arrival_age_s = None
                self.last_growth_sample_age_s = None
                if self.gsensor_measurement_ready:
                    self.gsensor_gate_reason = "waiting_for_fresh_valid_sample"
                return {
                    "accepted": True,
                    "kind": "sample",
                    "valid": False,
                    "frame_seq": sample.frame_seq,
                    "adapter_called": False,
                }
            self.last_valid_sample = sample
            self.last_valid_sample_received_monotonic = self._monotonic_clock()
            self.last_valid_sample_arrival_age_s = arrival_age
            self.gsensor_gate_reason = "ready"
            self.valid_sample_count += 1

        result = {
            "accepted": True,
            "kind": "sample",
            "valid": True,
            "frame_seq": sample.frame_seq,
            "adapter_called": False,
            "cached": True,
        }
        return result

    def _uses_live_growth_locked(self) -> bool:
        source = self.parameters.get(
            "growth_rate_source",
            self.parameters.get("exp_sim_G"),
        )
        if source is None:
            # Live experiment.start is the service contract. Older minimal
            # parameter snapshots therefore fail closed rather than silently
            # accepting identity-free GSensor traffic.
            return True
        return str(source).strip().lower() not in {"simulation", "simulated"}

    def _growth_input_availability_locked(
        self,
        *,
        now: float | None = None,
    ) -> tuple[bool, str, float | None]:
        if self.state != ControllerState.RUNNING:
            return False, "not_running", None
        if not self._uses_live_growth_locked():
            return True, "simulated_source", None
        if not self.gsensor_desired_enabled:
            return False, "disabled", None
        if not self.gsensor_enabled:
            return False, self.gsensor_gate_reason or "awaiting_gsensor_status", None
        if not self.gsensor_measurement_ready:
            return False, self.gsensor_gate_reason or "measurement_not_ready", None
        if self.last_valid_sample is None:
            return False, "waiting_for_fresh_valid_sample", None
        if self.last_valid_sample_received_monotonic is None:
            return False, "restart_requires_fresh_valid_sample", None
        if self.last_valid_sample_arrival_age_s is None:
            return False, "restart_requires_fresh_valid_sample", None
        now_value = self._monotonic_clock() if now is None else float(now)
        age = self.last_valid_sample_arrival_age_s + max(
            0.0,
            now_value - self.last_valid_sample_received_monotonic,
        )
        maximum_age = 2.0 * float(self.parameters.get("dt_G", 15.0))
        if age > maximum_age:
            return False, "stale_growth_sample", age
        return True, "ready", age

    def _control_tick_once(self, *, now: float | None = None) -> dict[str, Any]:
        """Execute one scheduler tick; exposed for deterministic integration tests."""

        with self._lock:
            if self.state != ControllerState.RUNNING:
                return {"executed": False, "reason": "not_running"}
            now_value = self._monotonic_clock() if now is None else float(now)
            dt = float(self.parameters.get("dt", 5.0))
            if not math.isfinite(dt) or dt <= 0:
                self.state = ControllerState.ERROR
                raise RuntimeError("Controller parameter dt must be finite and positive.")
            self.control_tick_count += 1
            tick_seq = self.control_tick_count
            elapsed = tick_seq * dt
            sample = self.last_valid_sample
            growth_available, growth_reason, age = self._growth_input_availability_locked(
                now=now_value
            )
            observed_growth_age = age
            self.last_growth_sample_age_s = observed_growth_age
            if self._uses_live_growth_locked() and not growth_available:
                # Never forward an unusable live sample to an arbitrary adapter.
                # Its total age remains visible in service status/tick results.
                sample = None
                age = None

            process_state: ProcessState | None = None
            if self.settings.opcua_enabled:
                try:
                    process_state = self.process_adapter.read_state()
                except Exception as exc:
                    self.process_error_count += 1
                    self.control_tick_error_count += 1
                    self.last_process_error = str(exc)
                    self.state = ControllerState.ERROR
                    self._next_tick_monotonic = None
                    self.process_adapter.disconnect()
                    raise RuntimeError(f"Controller process read failed: {exc}") from exc
                self.last_process_state = process_state.to_dict()
                self.last_process_error = None

            tick = ControllerTickInput(
                tick_seq=tick_seq,
                controller_dt_s=dt,
                elapsed_s=elapsed,
                growth_sample=sample,
                growth_sample_age_s=age,
                process_state=process_state,
            )
            try:
                fits_before = (self.adapter.adaptation_status() or {}).get("fit_count")
                output = self.adapter.step(tick)
                fits_after = (self.adapter.adaptation_status() or {}).get("fit_count")
                if fits_before is not None and fits_after is not None and fits_after > fits_before:
                    self.last_fit_success_at = utc_ts()
                if output is not None and not isinstance(output, ControllerStepResult):
                    raise TypeError(
                        "Controller adapter step() must return ControllerStepResult or None."
                    )
            except Exception as exc:
                self.adapter_error_count += 1
                self.control_tick_error_count += 1
                self.state = ControllerState.ERROR
                self._next_tick_monotonic = None
                self.process_adapter.disconnect()
                raise RuntimeError(f"Controller adapter step failed: {exc}") from exc

            output_status: dict[str, Any] | None = None
            if output is not None:
                process_write: ProcessWriteResult | None = None
                process_write_attempted = bool(
                    self.settings.opcua_enabled
                    and self.settings.opcua_write_enabled
                    and output.valid
                    and output.T_j_set is not None
                )
                process_write_error: str | None = None
                if process_write_attempted:
                    try:
                        process_write = self.process_adapter.write_jacket_setpoint(
                            output.T_j_set
                        )
                    except Exception as exc:
                        self.process_error_count += 1
                        self.control_tick_error_count += 1
                        self.last_process_error = str(exc)
                        process_write_error = str(exc)
                        self.state = ControllerState.ERROR
                        self._next_tick_monotonic = None
                        self.process_adapter.disconnect()
                    else:
                        self.last_process_write = process_write.to_dict()
                        self.last_process_error = None
                output_status = self._record_control_output_locked(
                    tick,
                    output,
                    process_state=process_state,
                    process_write=process_write,
                    process_write_attempted=process_write_attempted,
                    process_write_error=process_write_error,
                )
                if process_write_error is not None:
                    raise RuntimeError(f"Controller process write failed: {process_write_error}")
            if self.state == ControllerState.RUNNING:
                previous_deadline = self._next_tick_monotonic or now_value
                self._next_tick_monotonic = max(previous_deadline + dt, now_value + dt)
            self._try_persist_state_locked()
            result = {
                "executed": True,
                "tick_seq": tick_seq,
                "growth_frame_seq": sample.frame_seq if sample is not None else None,
                "growth_sample_age_s": observed_growth_age,
                "growth_input_available": growth_available,
                "growth_input_reason": growth_reason,
                "output_generated": output is not None,
            }
            if output_status is not None:
                result.update(output_status)
            return result

    def _record_control_output_locked(
        self,
        tick: ControllerTickInput,
        output: ControllerStepResult,
        *,
        process_state: ProcessState | None = None,
        process_write: ProcessWriteResult | None = None,
        process_write_attempted: bool = False,
        process_write_error: str | None = None,
    ) -> dict[str, Any]:
        computed_at = utc_ts()
        written: bool | None = None
        record = ControllerMeasurementRecord(
            run_id=str(self.current_run_id),
            tick=tick,
            result=output,
            computed_at=computed_at,
            adaptation_enabled=self.adaptation_enabled,
            adaptation_mode=self.adaptation_mode,
            process_state=process_state,
            process_write=process_write,
            process_write_attempted=process_write_attempted,
            process_write_error=process_write_error,
            # The tick, run ID, and its parameter snapshot share the service lock.
            control_target=(
                self.runtime_configuration_current["control_target"]
                if self.runtime_configuration_current is not None
                else _configured_control_target(self.parameters)
            ),
            runtime_revision=self.runtime_revision,
        )
        if self.measurement_writer is not None:
            try:
                write_controller_measurement(self.measurement_writer, record)
            except Exception as exc:
                written = False
                self.influx_write_failure_count += 1
                self.last_influx_error = str(exc)
                logger.warning(
                    "Could not persist Controller measurement for %s frame %s: %s",
                    self.current_run_id,
                    tick.tick_seq,
                    exc,
                )
            else:
                written = True
                self.influx_write_success_count += 1
                self.last_influx_write_at = computed_at
                self.last_influx_error = None
        elif self.settings.influx_enabled:
            written = False
            self.influx_write_failure_count += 1
            if self.last_influx_error is None:
                self.last_influx_error = (
                    "Controller InfluxDB persistence is enabled but no writer is available."
                )

        self.control_output_count += 1
        if not output.valid:
            self.invalid_control_output_count += 1
        self.last_control_output = {
            "run_id": self.current_run_id,
            "tick_seq": tick.tick_seq,
            "controller_dt_s": tick.controller_dt_s,
            "elapsed_s": tick.elapsed_s,
            "growth_frame_seq": (
                tick.growth_sample.frame_seq
                if tick.growth_sample is not None
                else None
            ),
            "growth_sample_age_s": tick.growth_sample_age_s,
            "computed_at": computed_at,
            "runtime_configuration": copy.deepcopy(self.runtime_configuration_current),
            "runtime_revision": self.runtime_revision,
            "result": output.to_dict(),
            "process_state": (
                process_state.to_dict() if process_state is not None else None
            ),
            "process_write_attempted": process_write_attempted,
            "process_write": (
                process_write.to_dict() if process_write is not None else None
            ),
            "process_write_error": process_write_error,
            "influxdb_written": written,
        }
        return {
            "output_generated": True,
            "output_valid": output.valid,
            "influxdb_written": written,
            "process_write_attempted": process_write_attempted,
            "process_write_succeeded": (
                process_write is not None if process_write_attempted else None
            ),
        }

    def _integration_status_locked(self) -> dict[str, dict[str, object]]:
        integrations = self.settings.integration_status()
        opcua = integrations["opcua"]
        opcua.update(copy.deepcopy(dict(self.process_adapter.status())))
        opcua.update(
            {
                "enabled": self.settings.opcua_enabled,
                "write_enabled": self.settings.opcua_write_enabled,
                "shadow_mode": (
                    self.settings.opcua_enabled
                    and not self.settings.opcua_write_enabled
                ),
                "configured": bool(self.settings.opcua_endpoint),
                "service_error_count": self.process_error_count,
                "service_last_error": self.last_process_error,
            }
        )
        influx = integrations["influxdb"]
        influx.update(
            {
                "writer_ready": self.measurement_writer is not None,
                "connected": (
                    self.influx_write_success_count > 0
                    and self.last_influx_error is None
                ),
                "write_success_count": self.influx_write_success_count,
                "write_failure_count": self.influx_write_failure_count,
                "last_write_at": self.last_influx_write_at,
                "last_error": self.last_influx_error,
                "initialization_error": self.influx_initialization_error,
            }
        )
        return integrations

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread_alive = bool(
                self._consumer_thread and self._consumer_thread.is_alive()
            )
            growth_available, growth_reason, current_growth_age = (
                self._growth_input_availability_locked()
            )
            control_target = (
                self.runtime_configuration_current["control_target"]
                if self.runtime_configuration_current is not None
                else _configured_control_target(self.parameters)
            )
            control_hold_reason = (
                growth_reason
                if self._uses_live_growth_locked()
                and control_target == "G"
                and not growth_available
                else None
            )
            return {
                "role": ROLE,
                "status": self.state.value,
                "active": self.state == ControllerState.RUNNING,
                "current_run_id": self.current_run_id,
                "parameter_version": self.parameter_version,
                "parameter_count": len(self.parameters),
                "control_target": control_target,
                "runtime_controls": {
                    "supported": self.runtime_configuration_current is not None,
                    "history_supported": self.runtime_history is not None,
                    "revision": self.runtime_revision,
                    "configuration": copy.deepcopy(self.runtime_configuration_current),
                    "last_result": copy.deepcopy(self.runtime_last_result),
                    "recent_events": [
                        copy.deepcopy(entry["result"])
                        for entry in list(self.runtime_events.values())[-50:]
                    ],
                    "history_error": self.runtime_history_error,
                },
                "parameters": copy.deepcopy(self.parameters),
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "last_frame_seq": self.last_frame_seq,
                "last_sample": copy.deepcopy(self.last_sample),
                "last_valid_sample": (
                    self.last_valid_sample.to_dict()
                    if self.last_valid_sample is not None
                    else None
                ),
                "growth_input": {
                    "source": "live_gsensor" if self._uses_live_growth_locked() else "simulated",
                    "available": growth_available,
                    "reason": growth_reason,
                    "control_hold_reason": control_hold_reason,
                    "requested_enabled": self.gsensor_desired_enabled,
                    "enabled": self.gsensor_enabled,
                    "measurement_ready": self.gsensor_measurement_ready,
                    "control_revision": self.gsensor_control_revision,
                    "control_event_id": self.gsensor_control_event_id,
                    "last_command_enabled": self.gsensor_last_command_enabled,
                    "initialization_generation": self.gsensor_initialization_generation,
                    "alignment_revision": self.gsensor_alignment_revision,
                    "sample_floor_frame_seq": self.gsensor_sample_floor_frame_seq,
                    "transition_occurred_at": self.gsensor_transition_occurred_at,
                    "last_status_at": self.gsensor_last_status_at,
                    "sample_age_s": current_growth_age,
                },
                "scheduler": {
                    "thread_alive": bool(
                        self._control_thread and self._control_thread.is_alive()
                    ),
                    "tick_count": self.control_tick_count,
                    "tick_error_count": self.control_tick_error_count,
                    "controller_dt_s": float(self.parameters.get("dt", 5.0)),
                    "growth_dt_s": float(self.parameters.get("dt_G", 15.0)),
                    "last_growth_sample_age_s": self.last_growth_sample_age_s,
                },
                "sample_counts": {
                    "valid": self.valid_sample_count,
                    "invalid": self.invalid_sample_count,
                    "duplicate": self.duplicate_sample_count,
                    "missing": self.missing_frame_count,
                },
                "seed_events": {
                    "count": self.seed_event_count,
                    "duplicate_count": self.duplicate_seed_event_count,
                    "last": copy.deepcopy(self.last_seed_event),
                },
                "adaptation": {
                    "fitting": ({**copy.deepcopy(self.adapter.adaptation_status()), "last_success_at": self.last_fit_success_at}
                                if self.adapter.adaptation_status() is not None else None),
                    "enabled": self.adaptation_enabled,
                    "active": (
                        self.state == ControllerState.RUNNING
                        and self.adaptation_enabled
                        and (
                            not self._uses_live_growth_locked()
                            or growth_available
                        )
                    ),
                    "mode": self.adaptation_mode,
                    "event_count": self.adaptation_event_count,
                    "duplicate_count": self.duplicate_adaptation_event_count,
                    "last_event": copy.deepcopy(self.last_adaptation_event),
                },
                "message_counts": {
                    "received": self.received_message_count,
                    "accepted": self.accepted_message_count,
                    "rejected": self.rejected_message_count,
                },
                "last_message": copy.deepcopy(self.last_message),
                "last_message_result": copy.deepcopy(self.last_message_result),
                "last_error": self.last_error,
                "consumer": {
                    "status": self.consumer_status,
                    "thread_alive": thread_alive,
                    "exchange": self.settings.rabbit_exchange,
                    "queue": self.settings.rabbit_queue,
                },
                "adapter": {
                    "class": (
                        f"{type(self.adapter).__module__}."
                        f"{type(self.adapter).__qualname__}"
                    ),
                    "safe_noop": isinstance(self.adapter, NoOpControllerAdapter),
                    "error_count": self.adapter_error_count,
                },
                "recovery": {
                    "status": self.recovery_status,
                    "error": self.recovery_error,
                    "state_file": str(self.state_path) if self.state_path else None,
                },
                "control_output_enabled": self.last_control_output is not None,
                "control_output_counts": {
                    "generated": self.control_output_count,
                    "invalid": self.invalid_control_output_count,
                },
                "last_control_output": copy.deepcopy(self.last_control_output),
                "integrations": self._integration_status_locked(),
            }


def _configured_control_target(parameters: Mapping[str, Any]) -> str | None:
    """Read the configured target type without inferring it from numeric results."""
    values = [
        parameters[key]
        for key in ("target", "control_target")
        if key in parameters
    ]
    if not values or any(
        not isinstance(value, str) or value not in ("sigma", "G")
        for value in values
    ):
        return None
    # Legacy callers may use control_target; conflicting aliases are ambiguous.
    return values[0] if all(value == values[0] for value in values) else None


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a positive integer.")
    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _timestamp_value(value: Any, name: str) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a timestamp string.")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone.")
    return parsed.astimezone(timezone.utc).timestamp()


def _utc_timestamp(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CONTROLLER_STATE_FILENAME",
    "ControllerService",
    "ControllerState",
    "ROLE",
]
