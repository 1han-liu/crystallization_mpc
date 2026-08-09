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
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.commands import (
    EXPERIMENT_START_COMMAND,
    EXPERIMENT_STOP_COMMAND,
    GROWTH_RATE_SAMPLE_MESSAGE,
    PARAMS_UPDATE_MESSAGE,
)
from crystallization_mpc.messaging.contracts import (
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
    ) -> None:
        self.settings = settings or ControllerSettings.from_env()
        self.adapter = adapter or load_controller_adapter(self.settings.adapter_spec)
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
            self.consumer_status = "stopped"
            # A process shutdown is not an experiment.stop. Preserve the
            # logical RUNNING state so a replacement container can recover it.
            if self.state != ControllerState.RUNNING:
                self._try_persist_state_locked()

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

            try:
                self.adapter.configure(copy.deepcopy(self.parameters), command.run_id)
                self.adapter.start()
            except Exception as exc:
                self.adapter_error_count += 1
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
            self.state = ControllerState.STOPPED
            self.stopped_at = command.stopped_at

        return {"accepted": True, "kind": "stop", "run_id": command.run_id}

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

            try:
                self.adapter.step(sample)
            except Exception as exc:
                self.adapter_error_count += 1
                self.state = ControllerState.ERROR
                raise RuntimeError(f"Controller adapter step failed: {exc}") from exc
            self.valid_sample_count += 1

        return {
            "accepted": True,
            "kind": "sample",
            "valid": True,
            "frame_seq": sample.frame_seq,
            "adapter_called": True,
        }

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
                "control_output_enabled": False,
                "last_control_output": None,
                "integrations": self.settings.integration_status(),
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
