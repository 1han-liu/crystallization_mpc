"""RabbitMQ-facing Controller lifecycle and input validation service."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
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
    EXPERIMENT_START_COMMAND,
    EXPERIMENT_STOP_COMMAND,
    GROWTH_RATE_SAMPLE_MESSAGE,
    PARAMS_UPDATE_MESSAGE,
)
from crystallization_mpc.messaging.contracts import (
    ControllerAddSeedPayload,
    ControllerAdaptationPayload,
    ExperimentStartPayload,
    ExperimentStopPayload,
    GrowthRateSamplePayload,
)
from crystallization_mpc.messaging.routing import bindings_for
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
    ) -> None:
        self.settings = settings or ControllerSettings.from_env()
        self.adapter = adapter or load_controller_adapter(self.settings.adapter_spec)
        self.process_adapter = process_adapter or self._build_process_adapter()
        self._consumer_runner = consumer_runner
        self._consumer_thread: threading.Thread | None = None
        self._consumer_stop = threading.Event()
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
        self.adaptation_event_count = 0
        self.duplicate_adaptation_event_count = 0
        self.last_adaptation_event: dict[str, Any] | None = None
        self._processed_adaptation_event_ids: set[str] = set()
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
            "current_run_id": self.current_run_id,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_frame_seq": self.last_frame_seq,
            "last_sample": copy.deepcopy(self.last_sample),
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
        }

    def _try_persist_state_locked(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            document = self._state_document_locked()
            temporary = self.state_path.with_name(
                f".{self.state_path.name}.{uuid4().hex}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(
                        document,
                        indent=2,
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self.state_path)
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
                        "The configured Controller adapter cannot restore a running experiment."
                    )
                self.adapter.set_adaptation(
                    self.adaptation_enabled,
                    self.adaptation_mode,
                    copy.deepcopy(self.last_adaptation_event),
                )
                if self.settings.opcua_enabled:
                    self.process_adapter.connect()
            self.recovery_status = "restored"
            self.recovery_error = None
        except Exception as exc:
            self.state = ControllerState.ERROR
            self.last_error = f"Controller recovery failed: {exc}"
            self.recovery_status = "error"
            self.recovery_error = str(exc)
            logger.exception("Could not restore Controller runtime state.")

    def start(self) -> None:
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

        with self._lock:
            self.last_message_result = copy.deepcopy(result)
            self._try_persist_state_locked()
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

            self.current_run_id = command.run_id
            self.started_at = command.started_at
            self.stopped_at = None
            self.last_frame_seq = None
            self.last_sample = None
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
            self.adaptation_event_count = 0
            self.duplicate_adaptation_event_count = 0
            self.last_adaptation_event = None
            self._processed_adaptation_event_ids.clear()

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
            if self.settings.opcua_enabled:
                try:
                    self.process_adapter.connect()
                except Exception as exc:
                    self.process_error_count += 1
                    self.last_process_error = str(exc)
                    self.process_adapter.disconnect()
                    self.state = ControllerState.ERROR
                    raise RuntimeError(
                        f"Controller process connection failed: {exc}"
                    ) from exc
            try:
                self.adapter.start()
            except Exception as exc:
                self.adapter_error_count += 1
                self.process_adapter.disconnect()
                self.state = ControllerState.ERROR
                raise RuntimeError(f"Controller adapter start failed: {exc}") from exc
            self.state = ControllerState.RUNNING

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
            if event.event_id in self._processed_adaptation_event_ids:
                self.duplicate_adaptation_event_count += 1
                return {
                    "accepted": True,
                    "duplicate": True,
                    "kind": "adaptation",
                    "event_id": event.event_id,
                }
            if self.state != ControllerState.RUNNING:
                raise ValueError("Controller is not running an experiment.")

            event_document = event.to_dict()
            no_change = (
                self.adaptation_enabled == event.enabled
                and self.adaptation_mode == event.mode
            )
            if not no_change:
                try:
                    self.adapter.set_adaptation(
                        event.enabled,
                        event.mode,
                        copy.deepcopy(event_document),
                    )
                except Exception as exc:
                    self.adapter_error_count += 1
                    self.state = ControllerState.ERROR
                    raise RuntimeError(
                        f"Controller adapter adaptation change failed: {exc}"
                    ) from exc

            self.adaptation_enabled = event.enabled
            self.adaptation_mode = event.mode
            self._processed_adaptation_event_ids.add(event.event_id)
            self.adaptation_event_count += 1
            self.last_adaptation_event = event_document

        return {
            "accepted": True,
            "kind": "adaptation",
            "event_id": event.event_id,
            "enabled": event.enabled,
            "mode": event.mode,
            "no_change": no_change,
            "adapter_called": not no_change,
        }

    def _accept_sample(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _positive_int(payload.get("frame_seq"), "frame_seq")
        sample = GrowthRateSamplePayload.from_mapping(payload)
        with self._lock:
            if self.state != ControllerState.RUNNING:
                raise ValueError("Controller is not running an experiment.")
            if sample.run_id != self.current_run_id:
                raise ValueError("growth_rate.sample run_id does not match current experiment.")
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
                return {
                    "accepted": True,
                    "kind": "sample",
                    "valid": False,
                    "frame_seq": sample.frame_seq,
                    "adapter_called": False,
                }

            process_state: ProcessState | None = None
            if self.settings.opcua_enabled:
                try:
                    process_state = self.process_adapter.read_state()
                except Exception as exc:
                    self.process_error_count += 1
                    self.last_process_error = str(exc)
                    self.state = ControllerState.ERROR
                    self.process_adapter.disconnect()
                    raise RuntimeError(f"Controller process read failed: {exc}") from exc
                self.last_process_state = process_state.to_dict()
                self.last_process_error = None

            try:
                output = self.adapter.step(sample, process_state)
                if output is not None and not isinstance(
                    output, ControllerStepResult
                ):
                    raise TypeError(
                        "Controller adapter step() must return "
                        "ControllerStepResult or None."
                    )
            except Exception as exc:
                self.adapter_error_count += 1
                self.state = ControllerState.ERROR
                self.process_adapter.disconnect()
                raise RuntimeError(f"Controller adapter step failed: {exc}") from exc
            self.valid_sample_count += 1

            output_status: dict[str, Any] | None = None
            if output is not None:
                process_write: ProcessWriteResult | None = None
                process_write_attempted = bool(
                    self.settings.opcua_enabled
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
                        self.last_process_error = str(exc)
                        process_write_error = str(exc)
                        self.state = ControllerState.ERROR
                        self.process_adapter.disconnect()
                    else:
                        self.last_process_write = process_write.to_dict()
                        self.last_process_error = None
                output_status = self._record_control_output_locked(
                    sample,
                    output,
                    process_state=process_state,
                    process_write=process_write,
                    process_write_attempted=process_write_attempted,
                    process_write_error=process_write_error,
                )
                if process_write_error is not None:
                    raise RuntimeError(
                        f"Controller process write failed: {process_write_error}"
                    )

        result = {
            "accepted": True,
            "kind": "sample",
            "valid": True,
            "frame_seq": sample.frame_seq,
            "adapter_called": True,
        }
        if output_status is not None:
            result.update(output_status)
        return result

    def _record_control_output_locked(
        self,
        sample: GrowthRateSamplePayload,
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
            sample=sample,
            result=output,
            computed_at=computed_at,
            adaptation_enabled=self.adaptation_enabled,
            adaptation_mode=self.adaptation_mode,
            process_state=process_state,
            process_write=process_write,
            process_write_attempted=process_write_attempted,
            process_write_error=process_write_error,
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
                    sample.run_id,
                    sample.frame_seq,
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
            "run_id": sample.run_id,
            "frame_seq": sample.frame_seq,
            "image_name": sample.image_name,
            "sample_processed_at": sample.processed_at,
            "computed_at": computed_at,
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
            return {
                "role": ROLE,
                "status": self.state.value,
                "active": self.state == ControllerState.RUNNING,
                "current_run_id": self.current_run_id,
                "parameter_version": self.parameter_version,
                "parameter_count": len(self.parameters),
                "parameters": copy.deepcopy(self.parameters),
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "last_frame_seq": self.last_frame_seq,
                "last_sample": copy.deepcopy(self.last_sample),
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
                    "enabled": self.adaptation_enabled,
                    "active": (
                        self.state == ControllerState.RUNNING
                        and self.adaptation_enabled
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


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a positive integer.")
    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


__all__ = [
    "CONTROLLER_STATE_FILENAME",
    "ControllerService",
    "ControllerState",
    "ROLE",
]
