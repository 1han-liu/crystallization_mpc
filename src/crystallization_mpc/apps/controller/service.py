"""MATLAB GUI execution order behind the existing Controller wire interface."""

from __future__ import annotations

from crystallization_mpc.infra.runtime_history import RuntimeHistory
from crystallization_mpc.apps.controller.numeric import encode_nonfinite, decode_nonfinite

import copy
import json
import logging
import math
import numpy as np
import os
import threading
import time
from dataclasses import replace
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
from crystallization_mpc.apps.controller.parameter_updates import OnlineParametersMixin
from crystallization_mpc.apps.controller.diagnostics import ControllerDiagnosticsSnapshot
from crystallization_mpc.apps.controller.process import (
    NoOpProcessAdapter,
    OpcUaNodeConfig,
    ProcessAdapter,
    ControllerReadError,
    ProcessConnectionError,
    ProcessMeasurementPending,
    ProcessWritePending,
    ProcessState,
    ProcessWriteResult,
)
from crystallization_mpc.apps.controller.rabbit_process import RabbitProcessAdapter, RabbitProcessConfig
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.tick import ControllerTickInput, ControllerInputValueError
from crystallization_mpc.apps.controller.telemetry import (
    ControllerMeasurementRecord,
    capture_controller_diagnostics,
    write_controller_measurement,
)
from crystallization_mpc.infra.influxdb.client import InfluxSettings
from crystallization_mpc.infra.influxdb.write import InfluxWriter
from crystallization_mpc.infra.influxdb.durable import DurableInfluxWriter, telemetry_path, mark_finalized
from crystallization_mpc.infra.influxdb.process_events import write_process_event
from crystallization_mpc.infra.storage_paths import system_path
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
from crystallization_mpc.messaging.run_parameters import (
    PARAMETERS_UPDATE, PARAMETERS_RESULT, parse_runtime_command,
)
from crystallization_mpc.messaging.schema import utc_ts

from .algorithm._logical import source_if as _source_truth

ROLE = "controller"
CONTROLLER_STATE_FILENAME = ".controller_runtime_state.json"
logger = logging.getLogger(__name__)


class ControllerState(str, Enum):
    IDLE = "idle"
    CONFIGURED = "configured"
    RUNNING = "running"
    RECOVERY_REQUIRED = "recovery_required"
    STOPPED = "stopped"
    ERROR = "error"


ConsumerRunner = Callable[..., None]


class ControllerService(OnlineParametersMixin):
    """Translate wire commands, then execute source assignments and loop stages."""

    def __init__(
        self,
        settings: ControllerSettings | None = None,
        adapter: ControllerAdapter | None = None,
        consumer_runner: ConsumerRunner = start_consumer,
        measurement_writer: InfluxWriter | None = None,
        process_adapter: ProcessAdapter | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        parameter_publisher: Any = None,
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
        # Producer timestamps are observational; a source G read occurs after control.
        self.growth_wait: dict[str, Any] | None = None
        self._growth_recovery_pending = False
        self.growth_stream_last_valid_at: str | None = None
        self._growth_stream_last_valid_monotonic: float | None = None
        self._growth_stream_arrival_age_s: float | None = None
        # G_active follows operator assignment; producer status is observational.
        self.gsensor_desired_enabled = False
        self.gsensor_enabled = False
        self.gsensor_measurement_ready = False
        self.gsensor_control_revision = 0
        self.gsensor_control_event_id: str | None = None
        self.gsensor_last_command_enabled: bool | None = None
        self.gsensor_last_command_requested_at: str | None = None
        self.gsensor_initialization_generation = 0
        self.gsensor_alignment_revision = 0
        self.gsensor_alignment_method: str | None = None
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
        self.last_read_warning: dict[str, Any] | None = None
        self.last_write_warning = None
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
        self.process_wait: dict[str, Any] | None = None
        self.process_write_wait: dict[str, Any] | None = None
        self.process_wait_count = 0
        self.process_activation_pending = False
        self.activation_client_unassigned = False
        self.initialization_status: dict[str, Any] = {
            "state": "not_started", "attempts": 0, "stage": None,
            "last_error": None, "completed_at": None,
        }
        self.last_control_output: dict[str, Any] | None = None
        # Observational, current-tick only. Never checkpointed or exposed through
        # the status/command API; acceptance can retain the full raw snapshot.
        self._last_committed_diagnostics: ControllerDiagnosticsSnapshot | None = None
        self._last_diagnostics_capture_error: str | None = None
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
                self.measurement_writer = DurableInfluxWriter(
                    InfluxSettings(
                        url=self.settings.influx_url,
                        token=self.settings.influx_token,
                        org=self.settings.influx_org,
                        bucket=self.settings.influx_bucket,
                    ), path=telemetry_path("controller", self.settings.experiment_root)
                )
            except Exception as exc:
                self.influx_initialization_error = str(exc)
                self.last_influx_error = str(exc)
                logger.warning("Could not initialize Controller InfluxDB writer: %s", exc)
        if isinstance(self.process_adapter, RabbitProcessAdapter):
            self.process_adapter.audit_writer = self.measurement_writer
            self.process_adapter.audit_required = False
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
        self.runtime_pending: dict[str, Any] | None = None
        self.runtime_refresh: dict[str, Any] = {"state": "completed", "event_id": None,
                                              "count": 0, "segment_id": 0}
        self._persistence_halted = False
        self.runtime_history_error: str | None = None
        self.recovery_status = "disabled"
        self.recovery_error: str | None = None
        self.safety_hold: dict[str, Any] | None = None
        self._configured_parameter_sections: dict[str, Any] | None = None
        self._initialize_online_parameters(parameter_publisher)
        self.state_path = (
            system_path(self.settings.experiment_root, CONTROLLER_STATE_FILENAME)
            if self.settings.experiment_root
            else None
        )
        # A new process corresponds to MATLAB clear; archives never activate it.
        self._adapter_configured = False
        self._parameters_received = False
        self._modified_queue = []
        self.controller_active = self.controller_active_old = False
        self._activation_wall_time = None
        self._last_iteration_start = None
        self._refresh_receipt_event_id = None
        self.runtime_history = RuntimeHistory(system_path(self.settings.experiment_root, ".controller_runtime_history.json")) if self.state_path else None
        for entry in self.runtime_events.values():
            self._archive_runtime_event(entry["command"], entry["result"])

    def _archive_runtime_event(self, command, result):
        if self.runtime_history is None:
            return
        try:
            self.runtime_history.save({"command": command, "status": result["status"],
                                       "result": result, "persistence": self.recovery_status})
            self.runtime_history_error = None
        except Exception as exc:
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
        if not self.settings.process_enabled:
            return NoOpProcessAdapter()
        return RabbitProcessAdapter(
            RabbitProcessConfig(
                rabbit_url=self.settings.rabbit_url,
                exchange=self.settings.rabbit_exchange,
                device_id=self.settings.process_device_id,
                measurement_name=self.settings.process_measurement_name,
                process_queue=self.settings.process_telemetry_queue,
                result_queue=self.settings.process_result_queue,
                sample_max_age_s=self.settings.process_sample_max_age_s,
                startup_timeout_s=self.settings.process_startup_timeout_s,
                command_ttl_s=self.settings.process_command_ttl_s,
                write_timeout_s=self.settings.process_write_timeout_s,
                node_ids=OpcUaNodeConfig(
                    namespace_index=self.settings.process_namespace_index,
                    T_j_set=self.settings.process_t_j_set_node,
                    T=self.settings.process_t_node,
                    T_j=self.settings.process_t_j_node,
                    c=self.settings.process_c_node,
                    count_middle=self.settings.process_count_middle_node,
                ).node_ids(),
                jacket_min_K=self.settings.process_jacket_min_K,
                jacket_max_K=self.settings.process_jacket_max_K,
                verify_tolerance_K=self.settings.process_verify_tolerance_K,
            )
        )

    def _state_document_locked(self) -> dict[str, Any]:
        adapter_state = self.adapter.export_state()
        if adapter_state is not None and not isinstance(adapter_state, Mapping):
            raise ValueError("Controller adapter export_state() must return an object or None.")
        return {
            "schema_version": 2,
            "updated_at": utc_ts(),
            "state": self.state.value,
            "safety_hold": copy.deepcopy(self.safety_hold),
            "parameters": copy.deepcopy(self.parameters),
            "parameter_sections": copy.deepcopy(self._configured_parameter_sections),
            "run_parameters": self._online_parameter_document(),
            "parameter_version": self.parameter_version,
            "runtime_controls": {
                "revision": self.runtime_revision,
                "configuration": copy.deepcopy(self.runtime_configuration_current),
                "events": copy.deepcopy(self.runtime_events),
                "last_result": copy.deepcopy(self.runtime_last_result),
                "pending": copy.deepcopy(self.runtime_pending),
                "refresh": copy.deepcopy(self.runtime_refresh),
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
                "alignment_method": self.gsensor_alignment_method,
                "sample_floor_frame_seq": self.gsensor_sample_floor_frame_seq,
                "transition_occurred_at": self.gsensor_transition_occurred_at,
                "last_status_at": self.gsensor_last_status_at,
                "gate_reason": self.gsensor_gate_reason,
                "stream_last_valid_at": self.growth_stream_last_valid_at,
                "growth_wait": copy.deepcopy(self.growth_wait),
                "growth_recovery_pending": self._growth_recovery_pending,
            },
            "last_message": copy.deepcopy(self.last_message),
            "last_message_result": copy.deepcopy(self.last_message_result),
            "last_error": self.last_error,
            "last_read_warning": copy.deepcopy(self.last_read_warning),
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
                "waiting": copy.deepcopy(self.process_wait),
                "write_wait": copy.deepcopy(self.process_write_wait),
                "wait_count": self.process_wait_count,
                "activation_pending": self.process_activation_pending,
                "activation_client_unassigned": self.activation_client_unassigned,
                "initialization": copy.deepcopy(self.initialization_status),
            },
            "scheduler": {
                "last_growth_sample_age_s": self.last_growth_sample_age_s,
            },
        }

    def _try_persist_state_locked(self):
        # Observation only. Failure cannot revoke assignments, require restart,
        # clear filters, or prevent the next source iteration.
        if self.state_path is None:
            return True
        temporary = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(f'.{self.state_path.name}.{uuid4().hex}.tmp')
            data = encode_nonfinite(self._state_document_locked())
            temporary.write_text(json.dumps(data,allow_nan=False,indent=2)+'\n')
            os.replace(temporary,self.state_path)
            self.recovery_status, self.recovery_error = 'saved', None
            return True
        except Exception as exc:
            self.recovery_status, self.recovery_error = 'persistence_error', str(exc)
            logger.warning('Controller observation archive unavailable: %s',exc)
            return False
        finally:
            if temporary is not None and temporary.exists():
                try: temporary.unlink()
                except OSError: pass


    def start(self, *, scheduler_enabled: bool = True, consumer_enabled: bool = True) -> None:
        """Start the consumer, optionally leaving ticks to an isolated test clock."""
        if scheduler_enabled and (not self._control_thread or not self._control_thread.is_alive()):
            self._control_stop.clear()
            self._control_thread = threading.Thread(
                target=self._control_forever,
                name="controller-control-clock",
                daemon=True,
            )
            self._control_thread.start()
        if not consumer_enabled:
            return
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
                self._reject_pending_runtime_locked("Controller shutdown before refresh.")
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
                    if self._parameters_received
                    else None
                )
            if deadline is None:
                self._control_wakeup.wait(0.25)
                self._control_wakeup.clear()
                continue
            delay = max(0.0, deadline - self._monotonic_clock())
            # The source interval is measured from the previous iteration start.
            if self._control_wakeup.wait(min(delay, 0.25)):
                self._control_wakeup.clear()
                continue
            if delay > 0.25:
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
                    if self.recovery_status != "error":
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
            if self.recovery_status == "error":
                # A later bus message must not hide the reason recovery failed
                # or make an incompatible checkpoint look ready to run.
                self.state = ControllerState.ERROR
                self.last_error = self._recovery_failure_reason_locked()
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

        if msg_type == "command" and name == PARAMETERS_UPDATE:
            if source != "central":
                raise ValueError("experiment.parameters.update must come from Central.")
            return self._update_run_parameters(payload)

        if msg_type == "status" and name == PARAMETERS_RESULT:
            if source != "gsensor":
                raise ValueError("experiment.parameters.result must come from GSensor.")
            return self._accept_parameter_result(payload)

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

    def _apply_parameters(self, payload):
        from .algorithm.parameters import build_parameters, parameters_to_workspace
        supplied = payload.get('params')
        if not isinstance(supplied, Mapping):
            raise TypeError('params.update requires an object.')
        self.parameter_version = payload.get('version')
        self._configured_parameter_sections = copy.deepcopy(payload.get('parameter_sections'))
        # Central's unchanged flat startup format is a template projection. Native
        # nested/dotted workspace maps are assigned without default repair.
        if 'params' in supplied:
            workspace = copy.deepcopy(dict(supplied))
        else:
            flat = self._complete_startup_parameters(supplied, self._configured_parameter_sections)
            workspace = parameters_to_workspace(build_parameters(flat))
        if not self._adapter_configured:
            defaults = dict(controller_active=False, controller_refresh_request=False,
                            add_seed_request=False, mark_seed=False, adaptive_disp=False,
                            pop_up_and_save_variables_refresh_request=False, inline_display=False,
                            adaptive=False, adaptive_mode='E_A', G_active=False)
            defaults.update(workspace)
            self.adapter.configure(defaults, self.current_run_id or 'gui-workspace')
            self._adapter_configured = True
            self.controller_active = self.controller_active_old = False
            self.process_activation_pending = False
            if self.initialization_status["state"] == "pending":
                self.initialization_status.update(state="stopped", stage=None)
        else:
            self.adapter.apply_modified(workspace)
        self._sync_workspace()
        self._reset_run_parameters(self._configured_parameter_sections, parameters=self.parameters)
        self._parameters_received = True
        if self.state == ControllerState.IDLE:
            self.state = ControllerState.CONFIGURED
        self._next_tick_monotonic = self._monotonic_clock() + self._get_dt()
        self._control_wakeup.set()
        return {'accepted': True, 'kind': 'params', 'parameter_version': self.parameter_version,
                'parameter_count': len(self.parameters)}


    def _recovery_failure_reason_locked(self) -> str:
        return f"Controller recovery failed: {self.recovery_error}"

    def _has_unended_started_run_locked(self) -> bool:
        """An immutable started identity remains owned until explicit End."""
        if not self.current_run_id or self.stopped_at is not None:
            return False
        try:
            _timestamp_utc(self.started_at, "started_at")
        except ValueError:
            return False
        return True

    def _start_experiment(self, payload):
        command = ExperimentStartPayload.from_mapping(payload)
        with self._lock:
            if not self._adapter_configured:
                raise ValueError('Controller has not received initial parameters.')
            if self.controller_active and self.current_run_id != command.run_id:
                raise ValueError('Start belongs to another active experiment.')
            if self.controller_active and self.current_run_id == command.run_id:
                return {'accepted': True, 'duplicate': True, 'kind': 'start'}
            self.current_run_id, self.started_at, self.stopped_at = command.run_id, command.started_at, None
            kernel = getattr(self.adapter, 'controller', None)
            if kernel is not None:
                kernel.run_id = self.current_run_id
            self.controller_active = True
            self.adapter.apply_modified({'controller_active': True, 'adaptive': command.adaptation_enabled,
                                         'adaptive_mode': command.adaptation_mode})
            self._sync_workspace()
            self.state = ControllerState.RUNNING
            self._next_tick_monotonic = self._monotonic_clock() + self._interval()
            self._control_wakeup.set()
            return {'accepted': True, 'kind': 'start', 'run_id': command.run_id}


    def _stop_experiment(self, payload):
        command = ExperimentStopPayload.from_mapping(payload)
        with self._lock:
            if command.run_id != self.current_run_id:
                raise ValueError('Stop belongs to another experiment.')
            self.controller_active = self.controller_active_old = False
            self._cancel_initialization()
            self.adapter.apply_modified({'controller_active': False})
            self.adapter.stop()
            self.state = ControllerState.STOPPED
            self.stopped_at = command.stopped_at
            # Source stop toggles activation; it does not destroy the workspace.
            self._modified_queue.clear()
            self.runtime_pending = None
            self.process_wait = self.process_write_wait = None
            self._try_persist_state_locked()
            try:
                self._mark_archive_finalized(command.run_id)
            except Exception as exc:
                logger.warning('Experiment archive finalization unavailable: %s', exc)
            return {'accepted': True, 'kind':'stop', 'run_id':command.run_id}


    def _mark_archive_finalized(self, run_id: str) -> None:
        mark_finalized(self.settings.experiment_root, run_id, "controller", self.control_tick_count,
                       enqueue_failures=self.influx_write_failure_count + int(self.process_adapter.status().get("audit_failure_count", 0)),
                       persistence_enabled=self.measurement_writer is not None)

    def _add_seed(self, payload):
        if payload.get('run_id') != self.current_run_id:
            raise ValueError('Seed message belongs to another experiment.')
        self.seed_event_count += 1
        self.last_seed_event = copy.deepcopy(dict(payload))
        return self.submit_operator_map({'add_seed_request': True}, event_id=payload.get('event_id'), command=payload)


    def _set_adaptation(self, payload):
        return self._update_runtime({'run_id':payload.get('run_id'),'event_id':payload.get('event_id'),
            'changes':{'adaptation_enabled':payload.get('enabled'),'adaptation_mode':payload.get('mode')},
            'requested_at':payload.get('requested_at')})


    def _update_runtime(self, payload):
        if payload.get('run_id') != self.current_run_id:
            raise ValueError('Runtime message belongs to another experiment.')
        aliases = {'control_target':'target','adaptation_enabled':'adaptive','adaptation_mode':'adaptive_mode'}
        values = {aliases.get(k,k):copy.deepcopy(v) for k,v in payload.get('changes',{}).items()}
        # target_set is an independent MATLAB workspace variable. Translate the
        # unchanged UI's named active setpoint into that supplied assignment.
        target = values.get('target', self.parameters.get('target'))
        if f'{target}_set' in values:
            values['target_set'] = values[f'{target}_set']
        elif 'target' in values:
            values['target_set'] = self.parameters.get(f'{target}_set')
        return self.submit_operator_map(values, event_id=payload.get('event_id'), command=payload)


    def _reject_pending_runtime_locked(self, reason):
        self._modified_queue.clear()
        self.runtime_pending = None
        self.run_parameter_pending = None


    def _check_growth_timeout_locked(self, *, now: float | None = None, boundary: str = "watchdog") -> bool:
        """Compatibility status hook; MATLAB has no live-G age watchdog.

        A G read is attempted after the control calculation. Its exception
        skips that iteration's remaining steps; successful unchanged values
        are not rejected by their age.
        """
        self.growth_wait = None
        self._growth_recovery_pending = False
        return False

    def _invalidate_growth_input_locked(
        self,
        *,
        reason: str,
        frame_seq: int | None = None,
        occurred_at: str | None = None,
    ) -> None:
        """Close the live-G gate and move both replay barriers forward."""

        if reason != "measurement_not_ready":
            self.last_sample = None
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

    def _set_gsensor_enabled(self, payload, *, enabled):
        if payload.get('run_id') != self.current_run_id:
            raise ValueError('G activation belongs to another experiment.')
        self.gsensor_desired_enabled = enabled
        self.gsensor_control_revision = payload.get('revision',self.gsensor_control_revision)
        self.gsensor_control_event_id = payload.get('event_id')
        self.gsensor_last_command_enabled = enabled
        self.gsensor_last_command_requested_at = payload.get('requested_at')
        self.adapter.set_g_active(enabled)
        return {'accepted':True,'kind':'gsensor_activation','enabled':self.gsensor_enabled,
                'requested_enabled':enabled,'measurement_ready':self.gsensor_measurement_ready,
                'control_revision':self.gsensor_control_revision,'control_event_id':self.gsensor_control_event_id}


    def _accept_growth_status(self, payload):
        if payload.get('run_id') != self.current_run_id:
            raise ValueError('G status belongs to another experiment.')
        self.gsensor_enabled = bool(payload.get('enabled',False))
        self.gsensor_measurement_ready = bool(payload.get('measurement_ready',False))
        self.gsensor_last_status_at = payload.get('occurred_at')
        self.gsensor_initialization_generation = payload.get('initialization_generation',0)
        self.gsensor_alignment_revision = payload.get('alignment_revision',0)
        self.gsensor_alignment_method = payload.get('alignment_method')
        self.gsensor_gate_reason = payload.get('status','unknown')
        # Producer status has no corresponding source control-flow statement.
        # A failed G read will fail at read_growth_rate, after control calculation.
        return {'accepted':True,'kind':'growth_status','controls_changed':False}


    def _accept_sample(self, payload):
        if payload.get('run_id') != self.current_run_id:
            raise ValueError('G sample belongs to another experiment.')
        self.last_sample = copy.deepcopy(dict(payload))
        from ._source_values import SourceGrowthSample
        self.last_valid_sample = SourceGrowthSample(payload)
        self.last_frame_seq = payload.get('frame_seq')
        self.valid_sample_count += int(payload.get('valid',True))
        self.invalid_sample_count += int(not payload.get('valid',True))
        return {'accepted':True,'kind':'sample','controls_changed':False}


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

    def _growth_input_availability_locked(self, *, now=None):
        # Observation only: producer health/ages cannot install a control gate.
        if not self._uses_live_growth_locked():
            return True, 'simulated_source', None
        if not self.gsensor_desired_enabled:
            return False, 'disabled', None
        return self.last_sample is not None, 'delivered_message' if self.last_sample is not None else 'no_message', None


    def _read_growth_sample_locked(self):
        if self.last_sample is None:
            raise ControllerReadError('No G message was delivered; this is not an observed OPC UA timeout.',stage='growth_read')
        return copy.deepcopy(self.last_sample)


    def confirm_process_recovery(self, payload):
        return {'accepted':False,'reason':'Controller has no cross-cycle write-confirmation state.'}


    def _control_tick_once(self, *, now=None):
        with self._lock:
            if not self._parameters_received:
                return {'executed':False,'reason':'initial_parameter_phase'}
            now = self._monotonic_clock() if now is None else now
            self._last_iteration_start = now
            stage, tick = 'receive_modified', None
            kernel = getattr(self.adapter,'controller',None)
            try:
                self._receive_modified()
                before_refresh = getattr(kernel, 'refresh_count', 0)
                stage = 'activation'
                from .algorithm._logical import source_if, source_not
                changed = source_if(np.not_equal(self.controller_active, self.controller_active_old))
                if changed:
                    # check_component_active returns its old flag only after
                    # evaluating the actual active flag successfully.
                    activate = source_if(self.controller_active)
                    self.controller_active_old = copy.deepcopy(self.controller_active)
                    if activate:
                        self._activate()
                    else:
                        self._cancel_initialization()
                        self.adapter.stop()
                else:
                    source_if(source_not(self.controller_active))
                    if source_if(self.controller_active) and self.process_activation_pending:
                        self._complete_activation()
                self._observe_controller_state()
                if (not source_if(self.controller_active == 1)
                        and self.initialization_status["state"] == "stopped"):
                    return {'executed': False, 'reason': 'controller_inactive'}
                stage = 'refresh'
                self.adapter.prepare_iteration()
                self._sync_workspace()
                if getattr(kernel, 'refresh_count', 0) > before_refresh:
                    event = self.runtime_events.get(self._refresh_receipt_event_id)
                    if event is not None:
                        event['result'].update(status='applied', phase='applied', refreshed=True,
                            effective_tick=self.control_tick_count+1, refresh_count=self.runtime_refresh.get('count'))
                        self.runtime_last_result = copy.deepcopy(event['result'])
                        self._archive_runtime_event(event['command'], event['result'])
                    self._refresh_receipt_event_id = None
                if not source_if(self.controller_active == 1):
                    return {'executed':False,'reason':'controller_inactive'}
                dt = self._get_dt()
                mode = self.parameters.get('run_type')
                elapsed = (self._wall_clock()-self._activation_wall_time if mode=='experiment'
                           and self._activation_wall_time is not None else self._run_elapsed_s+dt)
                stage = 'process_read'
                reader = None
                state = None
                if mode == 'experiment':
                    reader = getattr(self.process_adapter,'read_process_field',None)
                    if not callable(reader):
                        # Keep allocation/time before the legacy aggregate call.
                        def reader(name):
                            nonlocal state
                            if state is None: state = self.process_adapter.read_state()
                            value = getattr(state,name)
                            return value-self.parameters['C2K'] if name in ('T','T_j') else value
                tick = ControllerTickInput(self.control_tick_count+1,dt,elapsed,
                    process_reader=reader,
                    growth_reader=self._read_growth_sample_locked if self._uses_live_growth_locked() else None)
                output = self.adapter.step(tick)
                self._sync_workspace()
                if output is None:
                    return {'executed':False,'reason':'no_source_output','tick_seq':self.control_tick_count}
                stage = 'record'
                try:
                    result = self._record_control_output_locked(tick,output,process_state=state)
                except Exception as exc:
                    logger.warning('Controller observation unavailable: %s',exc)
                    result = {'output_generated':True,'influxdb_written':False,'observation_error':str(exc)}
                if mode == 'experiment':
                    stage = 'process_write'
                    try:
                        self.process_adapter.set_command_context(self.current_run_id,self.started_at,tick.tick_seq)
                        written = self.process_adapter.write_jacket_setpoint(output.T_j_set)
                    except Exception as exc:
                        result.update(self._observe_write_outcome(tick,outcome=getattr(exc,'write_outcome','unknown'),
                            attempted=getattr(exc,'command_sent',True) is not False,error=str(exc)))
                        self._warn(exc,stage,tick)
                    else:
                        result.update(self._observe_write_outcome(tick,outcome='success',attempted=True,result=written))
                return {'executed':True,'tick_seq':self.control_tick_count,**result}
            except Exception as exc:
                self._sync_workspace()
                if stage=='activation':
                    self.adapter.record_activation_read_failure(exc)
                self._observe_controller_state()
                self._warn(exc,getattr(exc,'stage',stage),tick)
                return {'executed':False,'reason':'source_exception','stage':getattr(exc,'stage',stage),
                        'tick_seq':self.control_tick_count,'output_generated':False,'process_write_attempted':False}
            finally:
                self.process_wait = self.process_write_wait = None
                self._next_tick_monotonic = now + self._interval()
                self._try_persist_state_locked()


    def _record_write_outcome_locked(
        self, tick: ControllerTickInput, *, outcome: str, attempted: bool,
        result: ProcessWriteResult | None = None, error: str | None = None,
    ) -> dict[str, Any]:
        """Record the write after the numerical point, without changing that point."""
        occurred_at = utc_ts()
        if error is not None:
            self.process_error_count += 1
            self.last_process_error = error
            self.last_write_warning = {
                "run_id": self.current_run_id, "tick_seq": tick.tick_seq,
                "elapsed_s": tick.elapsed_s, "stage": "process_write",
                "message": error, "occurred_at": occurred_at, "outcome": outcome,
            }
            logger.warning("Controller device write at cycle %s: %s; next cycle continues.", tick.tick_seq, error)
        else:
            self.last_process_write = result.to_dict() if result is not None else None
            self.last_process_error = None
            self.last_write_warning = None
        if self.last_control_output is not None:
            self.last_control_output.update(
                process_write_attempted=attempted, process_write_status=outcome,
                process_write=result.to_dict() if result is not None else None,
                process_write_error=error,
            )
        # A separate execution event preserves the pre-write calculation's
        # timestamp, covariance snapshot and applied parameter revision.
        if self.measurement_writer is not None:
            command: dict[str, Any] = {}
            try:
                candidate = self.process_adapter.status().get("last_command")
                if (isinstance(candidate, Mapping) and candidate.get("run_id") == self.current_run_id
                        and candidate.get("tick_seq") == tick.tick_seq):
                    command = dict(candidate)
            except Exception:
                pass
            payload = {
                **command, "command_id": command.get("command_id") or f"write-observation:{self.current_run_id}:{tick.tick_seq}",
                "run_id": self.current_run_id, "run_started_at": self.started_at,
                "tick_seq": tick.tick_seq, "status": outcome, "observed_at": occurred_at,
                "write_attempted": attempted, "error": error,
                "write_result": result.to_dict() if result is not None else None,
            }
            try:
                write_process_event(self.measurement_writer, payload, "controller", "cycle_write_outcome")
            except Exception as exc:
                self.influx_write_failure_count += 1
                self.last_influx_error = str(exc)
                logger.warning("Could not archive device write outcome at cycle %s: %s", tick.tick_seq, exc)
        return {
            "process_write_attempted": attempted, "process_write_status": outcome,
            "process_write_succeeded": True if outcome == "success" else None if outcome == "unknown" else False,
        }


    def _observe_write_outcome(self, tick, *, outcome, attempted, **kwargs):
        try:
            return self._record_write_outcome_locked(tick, outcome=outcome, attempted=attempted, **kwargs)
        except Exception as exc:
            logger.warning("Device outcome observation unavailable: %s", exc)
            return {"process_write_attempted": attempted, "process_write_status": outcome,
                    "process_write_succeeded": outcome == "success"}


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
        diagnostics: ControllerDiagnosticsSnapshot | None = None
        diagnostic_error: str | None = None
        if not output.valid:
            diagnostic_error = "invalid_output"
        elif process_write_error is not None:
            diagnostic_error = "process_write_failed"
        else:
            # Source calculations precede device writes. Diagnostics observe
            # the completed calculation without imposing a commit condition.
            diagnostics, diagnostic_error = capture_controller_diagnostics(
                self.adapter, run_id=str(self.current_run_id), tick=tick
            )
        self._last_committed_diagnostics = diagnostics
        self._last_diagnostics_capture_error = diagnostic_error
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
            measurement_parameter_revision=self.measurement_parameter_revision,
            diagnostics=diagnostics,
            diagnostics_capture_error=diagnostic_error,
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
                written = not getattr(self.measurement_writer, "asynchronous", False)
                self.influx_write_success_count += int(written)
                if written:
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
        # External acceptance observes a completed cycle through /api/status.
        # Keep its fit status and diagnostics aligned with that cycle, rather
        # than combining an old output with a later scheduler/fit status.
        # This is observational and must never turn a successful control tick
        # into a failure if an optional adapter cannot supply diagnostics.
        adaptation_fitting = None
        adaptation_capture_error = None
        if output.valid and process_write_error is None:
            adaptation_fitting, adaptation_capture_error = self._adaptation_snapshot_locked()
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
            "measurement_parameter_revision": self.measurement_parameter_revision,
            "runtime_refresh": copy.deepcopy(self.runtime_refresh),
            "adaptation_fitting": adaptation_fitting,
            "adaptation_capture_error": adaptation_capture_error,
            "diagnostics": diagnostics.to_dict() if diagnostics is not None else None,
            "diagnostics_capture_error": diagnostic_error,
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
            "influxdb_queued": bool(self.measurement_writer is not None and getattr(self.measurement_writer, "asynchronous", False) and self.last_influx_error is None),
        }
        return {
            "output_generated": True,
            "output_valid": output.valid,
            "influxdb_written": written,
            "influxdb_queued": bool(self.measurement_writer is not None and getattr(self.measurement_writer, "asynchronous", False) and self.last_influx_error is None),
            "process_write_attempted": process_write_attempted,
            "process_write_succeeded": (
                process_write is not None if process_write_attempted else None
            ),
        }

    def _adaptation_snapshot_locked(self) -> tuple[dict[str, Any] | None, str | None]:
        """Optional status observation cannot change a control or HTTP result."""
        try:
            snapshot = self.adapter.adaptation_status()
            if snapshot is not None and not isinstance(snapshot, Mapping):
                raise TypeError("adaptation_status must return a mapping or None")
            captured = copy.deepcopy(dict(snapshot)) if snapshot is not None else None
            # Non-finite numerical observations are losslessly tagged at JSON
            # boundaries; malformed structures remain observer-only failures.
            json.dumps(encode_nonfinite(captured), allow_nan=False)
            return captured, None
        except Exception as exc:
            return None, type(exc).__name__

    def _integration_status_locked(self) -> dict[str, dict[str, object]]:
        integrations = self.settings.integration_status()
        process = integrations["process"]
        process.update(copy.deepcopy(dict(self.process_adapter.status())))
        waiting = self.state == ControllerState.RUNNING and (
            self.process_wait is not None or self.process_write_wait is not None
            or process.get("waiting_for_data") is True
        )
        wait_metadata = self.process_wait or process.get("last_invalid_state") or {}
        process.update(
            {
                "enabled": self.settings.process_enabled,
                "write_enabled": self.settings.process_write_enabled,
                "shadow_mode": (
                    self.settings.process_enabled
                    and not self.settings.process_write_enabled
                ),
                "configured": bool(self.settings.rabbit_url and self.settings.rabbit_exchange),
                "transport": "rabbitmq",
                "write_enabled": self.parameters.get("run_type") == "experiment",
                "shadow_mode": False,
                "service_error_count": self.process_error_count,
                "service_last_error": self.last_process_error,
                "waiting_for_data": waiting,
                "wait_reason": wait_metadata.get("reason") if waiting else None,
                "waiting_since": wait_metadata.get("waiting_since") if waiting else None,
                "last_invalid_state": copy.deepcopy(wait_metadata) if wait_metadata else None,
                "wait_count": self.process_wait_count,
                "activation_pending": self.process_activation_pending,
                "activation_client_unassigned": self.activation_client_unassigned,
                "initialization": copy.deepcopy(self.initialization_status),
                "write_recovery": copy.deepcopy(self.process_write_wait),
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
        if isinstance(self.measurement_writer, DurableInfluxWriter):
            archive = self.measurement_writer.status(self.current_run_id)
            influx.update(archive=archive, write_success_count=archive["delivered_count"],
                          connected=bool(archive["delivered_count"] and not archive["last_error"]),
                          last_error=archive["last_error"] or self.last_influx_error)
        return integrations

    def status(self) -> dict[str, Any]:
        with self._lock:
            recovery_failed = self.recovery_status == "error"
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
            # G availability is diagnostic. It no longer installs a persistent
            # pre-control gate; the post-control read may abort that iteration.
            control_hold_reason = None
            if self._parameter_confirmation_holds_control():
                control_hold_reason = "parameter_confirmation"
            adaptation_fitting, adaptation_capture_error = self._adaptation_snapshot_locked()
            return {
                "role": ROLE,
                "status": ControllerState.ERROR.value if recovery_failed else self.state.value,
                "active": self.state == ControllerState.RUNNING and not recovery_failed,
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
                    "pending": copy.deepcopy(self.runtime_pending or self.run_parameter_pending),
                    "refresh": copy.deepcopy(self.runtime_refresh),
                    "recent_events": [
                        copy.deepcopy(entry["result"])
                        for entry in list(self.runtime_events.values())[-50:]
                    ],
                    "history_error": self.runtime_history_error,
                },
                "run_parameters": self._run_parameter_status(),
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
                    "parameter_revision": self.measurement_parameter_revision,
                    "control_event_id": self.gsensor_control_event_id,
                    "last_command_enabled": self.gsensor_last_command_enabled,
                    "initialization_generation": self.gsensor_initialization_generation,
                    "alignment_revision": self.gsensor_alignment_revision,
                    "alignment_method": self.gsensor_alignment_method,
                    "sample_floor_frame_seq": self.gsensor_sample_floor_frame_seq,
                    "transition_occurred_at": self.gsensor_transition_occurred_at,
                    "last_status_at": self.gsensor_last_status_at,
                    "stream_last_valid_at": self.growth_stream_last_valid_at,
                    "growth_wait": copy.deepcopy(self.growth_wait),
                    "sample_age_s": current_growth_age,
                },
                "scheduler": {
                    "deadline_set": self._next_tick_monotonic is not None,
                    "thread_alive": bool(
                        self._control_thread and self._control_thread.is_alive()
                    ),
                    "tick_count": self.control_tick_count,
                    "tick_error_count": self.control_tick_error_count,
                    "controller_dt_s": float(self._active_parameter("dt", 5.0)),
                    "growth_dt_s": float(self._active_parameter("dt_G", 15.0)),
                    "elapsed_s": self._run_elapsed_s,
                    "last_growth_sample_age_s": self.last_growth_sample_age_s,
                    "last_read_warning": copy.deepcopy(self.last_read_warning),
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
                    "fitting": ({**adaptation_fitting, "last_success_at": self.last_fit_success_at}
                                if adaptation_fitting is not None else None),
                    "capture_error": adaptation_capture_error,
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
                "last_error": (
                    self._recovery_failure_reason_locked()
                    if recovery_failed
                    else self.last_error
                ),
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
                    "requires_confirmation": self.state == ControllerState.RECOVERY_REQUIRED,
                    "requires_new_run": self.state == ControllerState.RECOVERY_REQUIRED,
                    "hold_reason": (self.safety_hold or {}).get("reason"),
                    "hold_detail": (self.safety_hold or {}).get("detail"),
                    "hold_occurred_at": (self.safety_hold or {}).get("occurred_at"),
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


    def _sync_workspace(self):
        kernel = getattr(self.adapter, "controller", None)
        if kernel is not None:
            self.parameters = copy.deepcopy(dict(kernel.params))
        try:
            config = self.adapter.runtime_configuration()
        except Exception:
            # A partial/empty map is still a successful map2base assignment.
            # A missing variable must fail when the source actually uses it,
            # not while formatting an optional status projection.
            config = None
        if isinstance(config, Mapping):
            self.runtime_configuration_current = dict(config)
            self.adaptation_enabled = config.get("adaptation_enabled", self.adaptation_enabled)
            self.adaptation_mode = config.get("adaptation_mode", self.adaptation_mode)
        try:
            workspace = self._workspace()
        except Exception as exc:
            workspace = {}
            logger.warning('Workspace observation unavailable: %s', exc)
        if "controller_active" in workspace and not isinstance(workspace["controller_active"], dict):
            self.controller_active = workspace["controller_active"]
        if "G_active" in workspace and not isinstance(workspace["G_active"], dict):
            self.gsensor_desired_enabled = workspace["G_active"]
        # Web experiment identity annotates this workspace; it never selects
        # an alternate control branch or restores an archived experiment.
        raw = getattr(kernel, "_root_workspace", {}) if kernel is not None else {}
        if "run_id" in raw:
            self.current_run_id = raw["run_id"]
            kernel.run_id = raw["run_id"]
        if "experiment_started_at" in raw:
            self.started_at = raw["experiment_started_at"]
        try:
            refresh = self.adapter.refresh_status()
        except Exception as exc:
            refresh = None
            logger.warning('Refresh observation unavailable: %s', exc)
        if isinstance(refresh, Mapping):
            self.runtime_refresh = copy.deepcopy(dict(refresh))
        if kernel is not None:
            self.control_tick_count = kernel.frame_index
            self._run_elapsed_s = kernel.elapsed_s


    def _interval(self):
        dt = self._get_dt()
        mode = self.parameters.get("run_type", self.parameters.get("exp_sim"))
        if mode == "simulation":
            return dt / 10.0
        if mode == "experiment":
            return dt
        raise UnboundLocalError("Unrecognized function or variable 'dt_sleep'.")


    def _receive_modified(self):
        if not self._modified_queue:
            return
        entry = self._modified_queue[0]
        try:
            self.adapter.apply_modified(entry['values'])
        except Exception as exc:
            # receive_modified catches map2base errors. Earlier assignments stay;
            # the unacknowledged map remains available at the next source loop.
            self._sync_workspace()
            logger.warning('Receiving modified parameters failed: %s', exc)
            return
        self._modified_queue.pop(0)
        self._sync_workspace()
        explicit_refresh = _source_truth(entry['values'].get('controller_refresh_request', False))
        if explicit_refresh:
            self._refresh_receipt_event_id = entry['event_id']
        event_id = entry['event_id']
        if event_id is not None:
            self.runtime_revision += 1
            result = self.runtime_events[event_id]['result']
            result.update(status='applied', phase='applied', controller_applied=True,
                          revision=self.runtime_revision, effective_tick=self.control_tick_count + 1,
                          elapsed_s=self._run_elapsed_s, applied_at=utc_ts(), refreshed=False,
                          configuration=copy.deepcopy(self.runtime_configuration_current),
                          parameter_values=self._actual_run_parameter_values())
            if explicit_refresh:
                result.update(status='pending', phase='requested', refreshed=False)
            self.runtime_last_result = copy.deepcopy(result)
            if entry['kind'] == 'parameters':
                for section in ('shared','controller'):
                    self.run_parameter_values[section].update(copy.deepcopy(entry['parameter_changes'].get(section,{})))
                sensor = entry['parameter_changes'].get('gsensor', {})
                if sensor:
                    pending = {'command': entry['command'], 'changes': {'gsensor': copy.deepcopy(sensor)},
                               'revision': self.runtime_revision, 'phase': 'prepare', 'controller_applied': True}
                    self.run_parameter_pending = pending
                    try:
                        self._send_parameter_phase(pending, 'prepare')
                    except Exception as exc:
                        pending.update(phase='failed', transport_error=str(exc))
                    if pending['phase'] == 'failed':
                        result.update(status='rejected', phase='failed', reason=pending.get('transport_error'),
                                      controller_applied=True, refreshed=False)
                    else:
                        result.update(status='pending', phase=pending['phase'], refreshed=False)
                self.run_parameter_last_result = copy.deepcopy(result)
            self._archive_runtime_event(entry['command'], result)
        self.runtime_pending = copy.deepcopy(self._modified_queue[0]) if self._modified_queue else None


    def submit_operator_map(self, values, *, event_id=None, command=None, parameter_changes=None, kind='runtime'):
        if not isinstance(values, Mapping):
            raise TypeError('Modified values must be a mapping.')
        if event_id in self.runtime_events:
            return {**copy.deepcopy(self.runtime_events[event_id]['result']), 'duplicate': True}
        entry = {'values': copy.deepcopy(dict(values)), 'event_id': event_id,
                 'command': copy.deepcopy(dict(command or {})),
                 'parameter_changes': copy.deepcopy(dict(parameter_changes or {})), 'kind': kind}
        self._modified_queue.append(entry)
        result = {'accepted': True, 'status': 'pending', 'kind': kind, 'event_id': event_id,
                  'run_id': self.current_run_id, 'revision': self.runtime_revision,
                  'configuration': copy.deepcopy(self.runtime_configuration_current), 'refreshed': False}
        if event_id is not None:
            self.runtime_events[event_id] = {'command': entry['command'], 'result': copy.deepcopy(result)}
        self.runtime_pending = copy.deepcopy(result)
        return result


    def _warn(self, error, stage, tick=None):
        self.control_tick_error_count += 1
        self.last_error = None
        warning = {"run_id": self.current_run_id, "tick_seq": self.control_tick_count,
                   "elapsed_s": self._run_elapsed_s, "stage": stage,
                   "message": str(error), "occurred_at": utc_ts()}
        if stage == "process_write": self.last_write_warning = warning
        else: self.last_read_warning = warning
        logger.warning("Controller %s: %s", stage, error)


    def _get_dt(self):
        from .process import is_matlab_empty
        value = self.parameters["dt"]
        if is_matlab_empty(value):
            return np.asarray([])
        return np.asarray(value) if isinstance(value, (list, tuple)) else value


    def _workspace(self):
        method = getattr(self.adapter, "workspace_snapshot", None)
        return dict(method()) if callable(method) else {}


    def request_refresh(self, event_id=None):
        return self.submit_operator_map({'controller_refresh_request': True}, event_id=event_id)


    def _activate(self):
        # The run is started exactly once. A failed initial read is retried by
        # _complete_activation without clearing history or restarting its clock.
        self.process_activation_pending = True
        self.initialization_status = {
            "state": "pending", "attempts": 0, "stage": "start",
            "last_error": None, "completed_at": None,
        }
        self.adapter.start()
        self._run_started_monotonic = self._monotonic_clock()
        self._activation_wall_time = self._wall_clock()
        self._run_elapsed_s = 0.0
        self.control_tick_count = 0
        self._complete_activation()


    def _cancel_initialization(self):
        if not self.process_activation_pending:
            return
        # A failed constructor may have left the startup refresh flag set.
        # Stop cancels that unfinished attempt rather than rebuilding while
        # inactive or applying its refresh receipt during a future run.
        self.adapter.apply_modified({'controller_refresh_request': False})
        self.process_activation_pending = False
        self._refresh_receipt_event_id = None
        self.initialization_status.update(state="stopped", stage=None)


    def _complete_activation(self):
        """Retry unfinished initialization in the same active experiment."""
        if not self.process_activation_pending:
            return
        self.initialization_status["attempts"] += 1
        try:
            if self.parameters.get('run_type') == 'experiment':
                self.initialization_status["stage"] = "connect"
                self.process_adapter.connect()
                reader = getattr(self.process_adapter, 'read_initial_field', None)
                if callable(reader):
                    for name in ('T','T_j','T_j_set','c','count_middle'):
                        self.initialization_status["stage"] = f"read_initial_{name}"
                        self.adapter.assign_initial_scalar(name, reader(name), raw_units=True)
                else:
                    # Legacy third-party adapters expose only one snapshot.
                    self.initialization_status["stage"] = "read_initial_state"
                    state = self.process_adapter.read_state()
                    for name in ('T','T_j','T_j_set','c','count_middle'):
                        self.adapter.assign_initial_scalar(name, getattr(state,name), raw_units=False)
                    self.last_process_state = state.to_dict()
            self.initialization_status["stage"] = "seed_template"
            self.adapter.complete_initialization()
            self.initialization_status["stage"] = "filter_initialization"
            self.adapter.request_refresh()
            self.adapter.prepare_iteration()
        except Exception as exc:
            self.initialization_status.update(state="pending", last_error=str(exc))
            raise
        self.process_activation_pending = False
        self.initialization_status.update(
            state="completed", stage=None, last_error=None, completed_at=utc_ts(),
        )


    def _observe_controller_state(self):
        # HTTP status has no counterpart in the source statement sequence.
        # Converting an invalid source flag here must not rethrow from catch.
        try:
            self.state = (ControllerState.RUNNING if _source_truth(self.controller_active) else
                          ControllerState.STOPPED if self.stopped_at is not None else ControllerState.CONFIGURED)
        except (ValueError, TypeError):
            pass


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
    return _timestamp_utc(value, name).timestamp()


def _timestamp_utc(value: Any, name: str) -> datetime:
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
    return parsed.astimezone(timezone.utc)


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
