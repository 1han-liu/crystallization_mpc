from __future__ import annotations

import logging
import os
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from crystallization_mpc.apps.ui_mode import resolve_ui_mode, ui_mode_payload
from crystallization_mpc.apps.central.experiments import CentralExperimentManager
from crystallization_mpc.apps.central.params import (
    ParameterValidationError,
    apply_derived_params,
    load_operation_meta,
    load_param_meta,
    load_params,
    save_params_document,
    validate_params_section,
)
from crystallization_mpc.apps.central.run_configuration import (
    ADAPTATION_MODES,
    CONTROLLER_MODES,
    CONTROL_TARGETS,
    GROWTH_RATE_SOURCES,
    RUN_CONFIGURATION_FILENAME,
    RUN_TYPES,
    RunConfiguration,
    RunConfigurationStore,
)
from crystallization_mpc.experiments import (
    ExperimentNotFoundError,
    ExperimentRegistryError,
    ExperimentStatus,
    InvalidExperimentIdentifierError,
    InvalidExperimentStateError,
)
from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.infra.rabbitmq.publisher import publish
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue
from crystallization_mpc.messaging.idgen import next_seq
from crystallization_mpc.messaging.commands import (
    CONTROLLER_ADD_SEED_COMMAND,
    CONTROLLER_ADAPTATION_SET_COMMAND,
    EXPERIMENT_MODE_LIVE,
    EXPERIMENT_SELECT_COMMAND,
    EXPERIMENT_START_COMMAND,
    EXPERIMENT_STOP_COMMAND,
    GROWTH_RATE_COMPLETED_MESSAGE,
    GROWTH_RATE_STATUS_MESSAGE,
    PARAMS_UPDATE_MESSAGE,
)
from crystallization_mpc.messaging.contracts import (
    ControllerAddSeedPayload,
    ControllerAdaptationPayload,
    ExperimentStartPayload,
    ExperimentStopPayload,
    GrowthRateStatus,
    GrowthRateStatusPayload,
)
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for, route
from crystallization_mpc.messaging.schema import build_envelope, utc_ts

ROLE = "central"
TARGET_VALUES = ("sigma", "G")
UI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = UI_DIR.parents[4]
DEFAULT_PARAMS_PATH = PROJECT_ROOT / "params_default.yaml"
DEFAULT_RUNTIME_PARAMS_PATH = PROJECT_ROOT / "params_runtime.yaml"
DEFAULT_PARAM_META_PATH = PROJECT_ROOT / "param_meta.yaml"
DEFAULT_OPERATION_META_PATH = PROJECT_ROOT / "operation_meta.yaml"
DEFAULT_EXPERIMENT_ROOT = PROJECT_ROOT / ".runtime" / "experiments"
LATEST_OVERLAY_FILENAME = "gsensor_detection_latest.jpg"
FINAL_OVERLAY_FILENAME = "gsensor_detection_final.jpg"
logger = logging.getLogger(__name__)


class TargetUpdate(BaseModel):
    target: Literal["sigma", "G"]


class ParamsUpdate(BaseModel):
    version: int = 1
    shared: Dict[str, Any] = Field(default_factory=dict)
    gsensor: Dict[str, Any] = Field(default_factory=dict)
    controller: Dict[str, Any] = Field(default_factory=dict)


class RunConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_type: Literal["experiment", "simulation"]
    controller_mode: Literal["MPC", "PI"]
    control_target: Literal["sigma", "G"]
    adaptation_enabled: bool
    adaptation_mode: Literal[
        "E_A",
        "k_0",
        "n",
        "E_A_and_k_0",
        "E_A_and_n",
        "k_0_and_n",
        "all",
    ]
    growth_rate_source: Literal[
        "live_gsensor",
        "simulated",
        "presaved_images",
    ]

class OperationValueUpdate(BaseModel):
    key: str
    value: Any


class ExperimentCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)


class AdaptationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class CentralApp:
    def __init__(
        self,
        url: Optional[str] = None,
        exchange: Optional[str] = None,
        queue_name: Optional[str] = None,
        include_broadcast: bool = True,
    ) -> None:
        self.url = url or os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")
        self.exchange = exchange or os.getenv("RABBIT_EXCHANGE", EXCHANGE)
        self.queue_name = queue_name or os.getenv("RABBIT_QUEUE", QUEUES[ROLE])
        self.include_broadcast = include_broadcast
        self._conn = None
        self._ch = None

    def _is_connection_open(self) -> bool:
        return self._conn is not None and bool(getattr(self._conn, "is_open", False))

    def _is_channel_open(self) -> bool:
        return self._ch is not None and bool(getattr(self._ch, "is_open", False))

    def connect(self, force: bool = False) -> None:
        if not force and self._is_connection_open() and self._is_channel_open():
            return
        if force:
            self.close()
        binding_keys = bindings_for(ROLE, include_broadcast=self.include_broadcast)
        self._conn, self._ch = connect(self.url)
        declare_exchange(self._ch, self.exchange)
        declare_queue(self._ch, self.queue_name, binding_keys, self.exchange)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.exception("Failed to close RabbitMQ connection cleanly.")
        self._conn = None
        self._ch = None

    def _require_channel(self):
        if not self._is_connection_open() or not self._is_channel_open():
            self.connect(force=True)
        return self._ch

    def _publish_with_reconnect(
        self,
        routing_key: str,
        payload: Dict[str, Any],
        persistent: bool = True,
    ) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                ch = self._require_channel()
                publish(ch, self.exchange, routing_key, payload, persistent=persistent)
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "RabbitMQ publish failed on attempt %s/2, reconnecting.",
                    attempt + 1,
                    exc_info=True,
                )
                self.connect(force=True)
        raise RuntimeError("RabbitMQ publish failed after reconnect.") from last_error

    def publish_params(
        self,
        shared: Dict[str, object],
        gsensor: Dict[str, object],
        controller: Dict[str, object],
        version: int,
    ) -> Dict[str, Dict[str, Any]]:
        seq = next_seq()
        messages: Dict[str, Dict[str, Any]] = {}

        if shared or gsensor:
            payload = {"version": version, "params": {**shared, **gsensor}}
            env = build_envelope(
                src=ROLE,
                dst="gsensor",
                msg_type="params",
                name=PARAMS_UPDATE_MESSAGE,
                seq=seq,
                payload=payload,
            )
            self._publish_with_reconnect(route(ROLE, "gsensor"), env, persistent=True)
            messages["gsensor"] = env

        if shared or controller:
            payload = {"version": version, "params": {**shared, **controller}}
            env = build_envelope(
                src=ROLE,
                dst="controller",
                msg_type="params",
                name=PARAMS_UPDATE_MESSAGE,
                seq=seq,
                payload=payload,
            )
            self._publish_with_reconnect(route(ROLE, "controller"), env, persistent=True)
            messages["controller"] = env

        return messages

    def build_experiment_start_command(
        self,
        experiment: Dict[str, Any],
        *,
        dst: str,
        adaptation_enabled: bool = False,
        adaptation_mode: str = "E_A",
        seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = ExperimentStartPayload(
            run_id=str(experiment["run_id"]),
            parameter_version=int(experiment["parameter_version"]),
            started_at=str(experiment["started_at"]),
            image_directory=str(experiment.get("image_directory", "images")),
            mode=EXPERIMENT_MODE_LIVE,
            adaptation_enabled=adaptation_enabled,
            adaptation_mode=adaptation_mode,
        )
        return build_envelope(
            src=ROLE,
            dst=dst,
            msg_type="command",
            name=EXPERIMENT_START_COMMAND,
            seq=next_seq() if seq is None else seq,
            payload=payload.to_dict(),
        )

    def publish_experiment_start_command(
        self,
        experiment: Dict[str, Any],
        *,
        adaptation_enabled: bool = False,
        adaptation_mode: str = "E_A",
    ) -> Dict[str, Dict[str, Any]]:
        seq = next_seq()
        messages: Dict[str, Dict[str, Any]] = {}
        for dst in ("gsensor", "controller"):
            env = self.build_experiment_start_command(
                experiment,
                dst=dst,
                adaptation_enabled=adaptation_enabled,
                adaptation_mode=adaptation_mode,
                seq=seq,
            )
            self._publish_with_reconnect(route(ROLE, dst), env, persistent=True)
            messages[dst] = env
        return messages

    def build_experiment_stop_command(
        self,
        run_id: str,
        *,
        dst: str,
        stopped_at: str,
        reason: str = "central_stop",
        seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = ExperimentStopPayload(
            run_id=run_id,
            stopped_at=stopped_at,
            reason=reason,
        )
        return build_envelope(
            src=ROLE,
            dst=dst,
            msg_type="command",
            name=EXPERIMENT_STOP_COMMAND,
            seq=next_seq() if seq is None else seq,
            payload=payload.to_dict(),
        )

    def publish_experiment_stop_command(
        self,
        run_id: str,
        *,
        reason: str = "central_stop",
    ) -> Dict[str, Dict[str, Any]]:
        seq = next_seq()
        stopped_at = utc_ts()
        messages: Dict[str, Dict[str, Any]] = {}
        for dst in ("gsensor", "controller"):
            env = self.build_experiment_stop_command(
                run_id,
                dst=dst,
                stopped_at=stopped_at,
                reason=reason,
                seq=seq,
            )
            self._publish_with_reconnect(route(ROLE, dst), env, persistent=True)
            messages[dst] = env
        return messages

    def build_controller_add_seed_command(
        self,
        run_id: str,
        *,
        event_id: str,
        added_at: str,
        seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = ControllerAddSeedPayload(
            run_id=run_id,
            event_id=event_id,
            added_at=added_at,
        )
        return build_envelope(
            src=ROLE,
            dst="controller",
            msg_type="command",
            name=CONTROLLER_ADD_SEED_COMMAND,
            seq=next_seq() if seq is None else seq,
            payload=payload.to_dict(),
        )

    def publish_controller_add_seed_command(
        self,
        run_id: str,
        *,
        event_id: str | None = None,
        added_at: str | None = None,
    ) -> Dict[str, Any]:
        env = self.build_controller_add_seed_command(
            run_id,
            event_id=event_id or uuid4().hex,
            added_at=added_at or utc_ts(),
        )
        self._publish_with_reconnect(
            route(ROLE, "controller"),
            env,
            persistent=True,
        )
        return env

    def build_controller_adaptation_command(
        self,
        run_id: str,
        *,
        event_id: str,
        enabled: bool,
        mode: str,
        requested_at: str,
        seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = ControllerAdaptationPayload(
            run_id=run_id,
            event_id=event_id,
            enabled=enabled,
            mode=mode,
            requested_at=requested_at,
        )
        return build_envelope(
            src=ROLE,
            dst="controller",
            msg_type="command",
            name=CONTROLLER_ADAPTATION_SET_COMMAND,
            seq=next_seq() if seq is None else seq,
            payload=payload.to_dict(),
        )

    def publish_controller_adaptation_command(
        self,
        run_id: str,
        *,
        enabled: bool,
        mode: str,
        event_id: str | None = None,
        requested_at: str | None = None,
    ) -> Dict[str, Any]:
        env = self.build_controller_adaptation_command(
            run_id,
            event_id=event_id or uuid4().hex,
            enabled=enabled,
            mode=mode,
            requested_at=requested_at or utc_ts(),
        )
        self._publish_with_reconnect(
            route(ROLE, "controller"),
            env,
            persistent=True,
        )
        return env

    def build_experiment_select_command(
        self,
        run_id: str,
        *,
        image_directory: str = "images",
        mode: str = EXPERIMENT_MODE_LIVE,
        seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return build_envelope(
            src=ROLE,
            dst="gsensor",
            msg_type="command",
            name=EXPERIMENT_SELECT_COMMAND,
            seq=next_seq() if seq is None else seq,
            payload={
                "run_id": run_id,
                "image_directory": image_directory,
                "mode": mode,
            },
        )

    def publish_experiment_select_command(
        self,
        run_id: str,
        *,
        image_directory: str = "images",
        mode: str = EXPERIMENT_MODE_LIVE,
    ) -> Dict[str, Any]:
        env = self.build_experiment_select_command(
            run_id,
            image_directory=image_directory,
            mode=mode,
        )
        self._publish_with_reconnect(route(ROLE, "gsensor"), env, persistent=True)
        return env

class CentralService:
    def __init__(self, publisher: CentralApp | None = None) -> None:
        self.ui_mode = resolve_ui_mode()
        self.default_params_path = Path(os.getenv("PARAMS_DEFAULT_FILE", str(DEFAULT_PARAMS_PATH)))
        self.params_path = Path(os.getenv("PARAMS_FILE", str(DEFAULT_RUNTIME_PARAMS_PATH)))
        self.param_meta_path = Path(os.getenv("PARAM_META_FILE", str(DEFAULT_PARAM_META_PATH)))
        self.operation_meta_path = Path(os.getenv("OPERATION_META_FILE", str(DEFAULT_OPERATION_META_PATH)))
        self.experiment_root = Path(
            os.getenv("EXPERIMENT_ROOT", str(DEFAULT_EXPERIMENT_ROOT))
        )
        self.experiments = CentralExperimentManager(
            self.experiment_root,
            host_root_display=os.getenv("EXPERIMENT_HOST_ROOT_DISPLAY"),
        )
        configured_target = os.getenv("CONTROL_TARGET", "sigma")
        if configured_target not in TARGET_VALUES:
            configured_target = "sigma"
        self.default_run_configuration = RunConfiguration(
            control_target=configured_target
        )
        run_configuration_path = Path(
            os.getenv(
                "RUN_CONFIGURATION_FILE",
                str(self.experiment_root / RUN_CONFIGURATION_FILENAME),
            )
        )
        self.run_configuration_store = RunConfigurationStore(run_configuration_path)
        self.run_configuration = self.run_configuration_store.load(
            self.default_run_configuration
        )
        self.target: Literal["sigma", "G"] = self.run_configuration.control_target  # type: ignore[assignment]
        self.publisher = publisher or CentralApp()
        self.operation_state = self._build_default_operation_state()
        self._sync_operation_state_from_run_configuration()
        self.controller_status_url = os.getenv(
            "CONTROLLER_STATUS_URL", "http://localhost:8002/api/status"
        )
        self.last_gsensor_status: Dict[str, Any] | None = None
        self.last_gsensor_status_received_at: str | None = None
        self.last_status_consumer_error: str | None = None
        self.last_rejected_status_error: str | None = None
        self.status_message_count = 0
        self.status_rejection_count = 0
        self._consumer_thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def _active_params_path(self) -> Path:
        if self.params_path.exists():
            return self.params_path
        return self.default_params_path

    def ui_config(self) -> Dict[str, str | bool]:
        return ui_mode_payload(self.ui_mode)

    def run_configuration_payload(self) -> Dict[str, Any]:
        return {
            "configuration": self.run_configuration.to_dict(),
            "defaults": self.default_run_configuration.to_dict(),
            "choices": {
                "run_type": list(RUN_TYPES),
                "controller_mode": list(CONTROLLER_MODES),
                "control_target": list(CONTROL_TARGETS),
                "adaptation_mode": list(ADAPTATION_MODES),
                "growth_rate_source": list(GROWTH_RATE_SOURCES),
            },
            "saved": self.run_configuration_store.updated_at is not None,
            "updated_at": self.run_configuration_store.updated_at,
            "locked": self._run_configuration_locked(),
            "source_file": str(self.run_configuration_store.path),
        }

    def update_run_configuration(
        self,
        payload: RunConfigurationUpdate | Dict[str, Any],
    ) -> Dict[str, Any]:
        self._require_run_configuration_editable()
        if isinstance(payload, RunConfigurationUpdate):
            raw = {
                "run_type": payload.run_type,
                "controller_mode": payload.controller_mode,
                "control_target": payload.control_target,
                "adaptation_enabled": payload.adaptation_enabled,
                "adaptation_mode": payload.adaptation_mode,
                "growth_rate_source": payload.growth_rate_source,
            }
        else:
            raw = dict(payload)
        configuration = RunConfiguration.from_mapping(raw)
        changed = configuration != self.run_configuration
        if changed:
            self.run_configuration_store.save(configuration)
            self.run_configuration = configuration
            self.target = configuration.control_target  # type: ignore[assignment]
            self._sync_operation_state_from_run_configuration()
        result = self.run_configuration_payload()
        result["saved"] = True
        result["changed"] = changed
        return result

    def _run_configuration_locked(self) -> bool:
        run_id = self.experiments.current_run_id()
        if run_id is None:
            return False
        manifest = self.experiments.registry.get(run_id)
        return manifest.status not in {
            ExperimentStatus.CREATED,
            ExperimentStatus.COMPLETED,
            ExperimentStatus.ERROR,
        }

    def _require_run_configuration_editable(self) -> None:
        if self._run_configuration_locked():
            raise InvalidExperimentStateError(
                "Run configuration is locked after an experiment starts."
            )

    def _sync_operation_state_from_run_configuration(self) -> None:
        configuration = self.run_configuration
        growth_source_to_legacy = {
            "live_gsensor": "experiment",
            "simulated": "simulation",
            "presaved_images": "experiment_with_presaved",
        }
        self.operation_state.update(
            {
                "mode": configuration.controller_mode,
                "exp_sim": configuration.run_type,
                "target": configuration.control_target,
                "adaptive": configuration.adaptation_enabled,
                "adaptive_mode": configuration.adaptation_mode,
                "exp_sim_G": growth_source_to_legacy[
                    configuration.growth_rate_source
                ],
            }
        )

    def start(self) -> None:
        self.publisher.connect()
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._consumer_thread = threading.Thread(
            target=self._consume_forever,
            name="central-rabbitmq-consumer",
            daemon=True,
        )
        self._consumer_thread.start()

    def stop(self) -> None:
        self.publisher.close()

    def _consume_forever(self) -> None:
        while True:
            try:
                start_consumer(
                    url=self.publisher.url,
                    exchange=self.publisher.exchange,
                    queue_name=self.publisher.queue_name,
                    binding_keys=bindings_for(ROLE),
                    on_message=self.on_message,
                )
            except Exception as exc:
                with self._lock:
                    self.last_status_consumer_error = str(exc)
                logger.exception("Central RabbitMQ consumer stopped; retrying.")
                time.sleep(5)

    def on_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if message.get("src") != "gsensor" or message.get("dst") != ROLE:
                raise ValueError("Central growth-rate status must come from Gsensor.")
            if message.get("msg_type") != "status":
                raise ValueError("Central only accepts Gsensor status messages here.")
            name = message.get("name")
            if name not in {
                GROWTH_RATE_STATUS_MESSAGE,
                GROWTH_RATE_COMPLETED_MESSAGE,
            }:
                raise ValueError(f"Unsupported Gsensor status message: {name!r}.")
            payload = GrowthRateStatusPayload.from_mapping(message.get("payload", {}))
            if (
                name == GROWTH_RATE_COMPLETED_MESSAGE
                and payload.status != GrowthRateStatus.COMPLETED
            ):
                raise ValueError("growth_rate.completed must use status='completed'.")
            self._apply_gsensor_status(payload)
        except Exception as exc:
            with self._lock:
                self.last_rejected_status_error = str(exc)
                self.status_rejection_count += 1
            logger.warning("Central rejected Gsensor status: %s", exc)
            return {"accepted": False, "reason": str(exc)}

        with self._lock:
            self.last_gsensor_status = _retain_last_gsensor_frame(
                self.last_gsensor_status,
                payload.to_dict(),
            )
            self.last_gsensor_status_received_at = utc_ts()
            self.last_status_consumer_error = None
            self.status_message_count += 1
        return {"accepted": True, "status": payload.status.value}

    def _apply_gsensor_status(self, payload: GrowthRateStatusPayload) -> None:
        run_id = self.experiments.current_run_id()
        if run_id is None or payload.run_id != run_id:
            raise ValueError("Gsensor status run_id does not match the current experiment.")

        status = payload.status
        if status == GrowthRateStatus.ERROR:
            self.experiments.registry.mark_error(
                run_id,
                error=payload.error or "Gsensor reported an error.",
            )
            with self._lock:
                self.operation_state["experiment_active"] = False
            return

        if status == GrowthRateStatus.COMPLETED:
            manifest = self.experiments.registry.get(run_id)
            if manifest.status != ExperimentStatus.STOPPING:
                self.experiments.request_stop(run_id)
            self.experiments.finish(run_id)
            with self._lock:
                self.operation_state["experiment_active"] = False
            return

        target_by_status = {
            GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE: ExperimentStatus.WAITING_FOR_INITIAL_IMAGE,
            GrowthRateStatus.INITIALIZING: ExperimentStatus.INITIALIZING,
            GrowthRateStatus.BASELINE_READY: ExperimentStatus.INITIALIZING,
            GrowthRateStatus.MEASURING: ExperimentStatus.MEASURING,
            GrowthRateStatus.STOPPING: ExperimentStatus.STOPPING,
            GrowthRateStatus.STOPPED: ExperimentStatus.STOPPING,
        }
        target = target_by_status.get(status)
        if target is None:
            return
        self._advance_experiment_status(run_id, target)
        with self._lock:
            self.operation_state["experiment_active"] = target not in {
                ExperimentStatus.STOPPING,
            }

    def _advance_experiment_status(
        self,
        run_id: str,
        target: ExperimentStatus,
    ) -> None:
        ordered = [
            ExperimentStatus.STARTING,
            ExperimentStatus.WAITING_FOR_INITIAL_IMAGE,
            ExperimentStatus.INITIALIZING,
            ExperimentStatus.MEASURING,
            ExperimentStatus.STOPPING,
        ]
        manifest = self.experiments.registry.get(run_id)
        if manifest.status == target:
            return
        if manifest.status in {ExperimentStatus.COMPLETED, ExperimentStatus.ERROR}:
            raise InvalidExperimentStateError(
                f"Cannot apply Gsensor status to {manifest.status.value} experiment."
            )
        current_index = ordered.index(manifest.status)
        target_index = ordered.index(target)
        if target_index < current_index:
            return
        for next_status in ordered[current_index + 1 : target_index + 1]:
            self.experiments.transition(run_id, next_status)

    def controller_status(self) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(self.controller_status_url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Controller status response must be an object.")
            return {"available": True, **payload, "error": None}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "available": False,
                "status": "unavailable",
                "active": False,
                "error": str(exc),
            }

    def system_status(self) -> Dict[str, Any]:
        experiments = self.experiments.list()
        current_run_id = experiments.get("current_run_id")
        current = next(
            (
                item
                for item in experiments.get("experiments", [])
                if item.get("run_id") == current_run_id
            ),
            None,
        )
        with self._lock:
            gsensor = {
                "last_status": self.last_gsensor_status,
                "received_at": self.last_gsensor_status_received_at,
                "consumer_error": self.last_status_consumer_error,
                "last_rejection_error": self.last_rejected_status_error,
                "message_count": self.status_message_count,
                "rejection_count": self.status_rejection_count,
            }
        return {
            "current_experiment": current,
            "experiments": experiments,
            "gsensor": gsensor,
            "controller": self.controller_status(),
        }
    def overlay_path(self, run_id: str, kind: str) -> Path:
        if kind not in {"latest", "final"}:
            raise ValueError("Overlay kind must be 'latest' or 'final'.")
        self.experiments.registry.get(run_id)
        run_directory = (self.experiment_root / run_id).resolve()
        filename = LATEST_OVERLAY_FILENAME if kind == "latest" else FINAL_OVERLAY_FILENAME
        path = (run_directory / filename).resolve()
        try:
            path.relative_to(run_directory)
        except ValueError as exc:
            raise InvalidExperimentIdentifierError("Overlay path escapes experiment.") from exc
        if not path.is_file():
            raise ExperimentNotFoundError(f"{kind.capitalize()} overlay is not available yet.")
        return path

    def load_params(self) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
        return load_params(str(self._active_params_path()))

    def load_default_params(
        self,
    ) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
        return load_params(str(self.default_params_path))

    def load_param_meta(self) -> Dict[str, Dict[str, Any]]:
        return load_param_meta(str(self.param_meta_path))

    def load_operation_meta(self) -> list[Dict[str, Any]]:
        return load_operation_meta(str(self.operation_meta_path))

    def _build_default_operation_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {}
        for section in self.load_operation_meta():
            for item in section.get("items", []):
                key = str(item.get("key", ""))
                if not key:
                    continue
                state[key] = item.get("default")
        state["target"] = self.target
        return state

    def update_operation_value(self, key: str, value: Any) -> Dict[str, Any]:
        legacy_fields = {
            "mode": "controller_mode",
            "exp_sim": "run_type",
            "target": "control_target",
            "adaptive": "adaptation_enabled",
            "adaptive_mode": "adaptation_mode",
        }
        if key == "exp_sim_G":
            legacy_growth_sources = {
                "experiment": "live_gsensor",
                "simulation": "simulated",
                "experiment_with_presaved": "presaved_images",
            }
            if value not in legacy_growth_sources:
                raise ValueError("Unsupported legacy growth-rate source.")
            field = "growth_rate_source"
            value = legacy_growth_sources[value]
        else:
            field = legacy_fields.get(key)
        if field is not None:
            updated = self.run_configuration.to_dict()
            updated[field] = value
            result = self.update_run_configuration(updated)
            return {
                "saved": True,
                "key": key,
                "value": self.operation_state.get(key),
                "run_configuration": result,
            }
        self.operation_state[key] = value
        return {
            "saved": True,
            "key": key,
            "value": self.operation_state.get(key),
        }

    def params_payload(self) -> Dict[str, Any]:
        shared, gsensor, controller, version = self.load_params()
        default_shared, default_gsensor, default_controller, default_version = (
            self.load_default_params()
        )
        return {
            "version": version,
            "shared": shared,
            "gsensor": gsensor,
            "controller": controller,
            "defaults": {
                "version": default_version,
                "shared": default_shared,
                "gsensor": default_gsensor,
                "controller": default_controller,
            },
            "meta": self.load_param_meta(),
            "source_file": str(self._active_params_path()),
            "runtime_file": str(self.params_path),
            "status": self.parameter_status(
                shared=shared,
                gsensor=gsensor,
                controller=controller,
                version=version,
                defaults=(default_shared, default_gsensor, default_controller),
            ),
        }

    def parameter_status(
        self,
        *,
        shared: Dict[str, object] | None = None,
        gsensor: Dict[str, object] | None = None,
        controller: Dict[str, object] | None = None,
        version: int | None = None,
        defaults: Tuple[
            Dict[str, object],
            Dict[str, object],
            Dict[str, object],
        ]
        | None = None,
    ) -> Dict[str, Any]:
        if shared is None or gsensor is None or controller is None or version is None:
            shared, gsensor, controller, version = self.load_params()
        if defaults is None:
            default_shared, default_gsensor, default_controller, _ = self.load_default_params()
            defaults = (default_shared, default_gsensor, default_controller)

        using_defaults = (shared, gsensor, controller) == defaults
        saved_at = None
        if self.params_path.is_file():
            modified_at = datetime.fromtimestamp(
                self.params_path.stat().st_mtime,
                tz=timezone.utc,
            )
            saved_at = modified_at.isoformat(timespec="seconds").replace("+00:00", "Z")

        applied_run_id = None
        applied_at = None
        current_run_id = self.experiments.current_run_id()
        if current_run_id is not None:
            try:
                manifest = self.experiments.registry.get(current_run_id)
            except ExperimentRegistryError:
                manifest = None
            if (
                manifest is not None
                and manifest.params_snapshot_file
                and manifest.parameter_version == version
            ):
                applied_run_id = current_run_id
                applied_at = manifest.started_at

        if applied_run_id is not None:
            kind = "applied"
            message = f"Applied to {applied_run_id} · version {version}"
        elif using_defaults:
            kind = "using_defaults"
            message = f"Using defaults · version {version}"
        else:
            kind = "draft_saved"
            message = f"Draft saved · version {version}"

        return {
            "kind": kind,
            "message": message,
            "using_defaults": using_defaults,
            "version": version,
            "saved_at": saved_at,
            "applied_run_id": applied_run_id,
            "applied_at": applied_at,
        }

    def save_params(
        self,
        payload: ParamsUpdate,
        *,
        allow_changes: bool = True,
        allow_locked: bool = False,
    ) -> Dict[str, Any]:
        if not allow_locked:
            self._require_parameter_draft_editable()
        current_shared, current_gsensor, current_controller, current_version = (
            self.load_params()
        )
        if int(payload.version) != current_version:
            raise ParameterValidationError(
                "The parameter draft changed on the server. Reload it before saving."
            )
        default_shared, default_gsensor, default_controller, _ = self.load_default_params()
        meta = self.load_param_meta()
        shared = validate_params_section(
            "shared", payload.shared, default_shared, meta
        )
        gsensor = validate_params_section(
            "gsensor", payload.gsensor, default_gsensor, meta
        )
        controller = validate_params_section(
            "controller", payload.controller, default_controller, meta
        )
        changed = (shared, gsensor, controller) != (
            current_shared,
            current_gsensor,
            current_controller,
        )
        if changed and not allow_changes:
            raise InvalidExperimentStateError(
                "This experiment has already started; its parameter snapshot is immutable."
            )
        if changed:
            version = current_version + 1
            save_params_document(
                str(self.params_path),
                version=version,
                shared=shared,
                gsensor=gsensor,
                controller=controller,
            )
        result = self.params_payload()
        result["saved"] = True
        result["changed"] = changed
        return result

    def _require_parameter_draft_editable(self) -> None:
        run_id = self.experiments.current_run_id()
        if run_id is None:
            return
        manifest = self.experiments.registry.get(run_id)
        if manifest.status not in {
            ExperimentStatus.CREATED,
            ExperimentStatus.COMPLETED,
            ExperimentStatus.ERROR,
        }:
            raise InvalidExperimentStateError(
                "Parameters are locked after an experiment starts. Create a new experiment "
                "before preparing another parameter draft."
            )

    def reset_params_to_defaults(self) -> Dict[str, Any]:
        shared, gsensor, controller, _default_version = self.load_default_params()
        _current_shared, _current_gsensor, _current_controller, current_version = (
            self.load_params()
        )
        payload = ParamsUpdate(
            version=current_version,
            shared=shared,
            gsensor=gsensor,
            controller=controller,
        )
        return self.save_params(payload)

    def preview_publish_payload(self) -> Dict[str, Any]:
        shared, gsensor, controller, version = self.load_params()
        shared, controller, derived = apply_derived_params(shared, controller, target=self.target)
        return {
            "version": version,
            "target": self.target,
            "run_configuration": self.run_configuration.to_dict(),
            "shared": shared,
            "gsensor": gsensor,
            "controller": controller,
            "derived": derived,
        }

    def create_experiment(self, *, label: str | None = None) -> Dict[str, Any]:
        """Prepare a new run without starting either experiment service."""

        self._require_experiment_switch_allowed()
        experiment = self.experiments.create(label=label)
        command = self.publisher.publish_experiment_select_command(
            experiment["run_id"],
            image_directory=experiment["image_directory"],
        )
        return {**experiment, "selection_command": command}

    def select_experiment(self, run_id: str) -> Dict[str, Any]:
        self._require_experiment_switch_allowed(run_id)
        experiment = self.experiments.select(run_id)
        command = self.publisher.publish_experiment_select_command(
            experiment["run_id"],
            image_directory=experiment["image_directory"],
        )
        return {**experiment, "selection_command": command}

    def _require_experiment_switch_allowed(self, run_id: str | None = None) -> None:
        current_run_id = self.experiments.current_run_id()
        if current_run_id is None:
            return
        if run_id is not None and run_id == current_run_id:
            return
        current = self.experiments.registry.get(current_run_id)
        if current.status in {ExperimentStatus.COMPLETED, ExperimentStatus.ERROR}:
            return
        raise InvalidExperimentStateError(
            "End the current experiment before creating or switching experiments."
        )

    def start_experiment(self, params: ParamsUpdate | None = None) -> Dict[str, Any]:
        run_id = self.experiments.current_run_id()
        if run_id is None:
            raise InvalidExperimentStateError(
                "Create or select an experiment before starting it."
            )
        manifest = self.experiments.registry.get(run_id)
        if manifest.status not in {ExperimentStatus.CREATED, ExperimentStatus.STARTING}:
            raise InvalidExperimentStateError(
                f"Cannot start experiment {run_id} from state {manifest.status.value}."
            )
        if params is not None:
            self.save_params(
                params,
                allow_changes=manifest.status == ExperimentStatus.CREATED,
                allow_locked=manifest.status == ExperimentStatus.STARTING,
            )
        params_snapshot = self.preview_publish_payload()
        if (
            manifest.status == ExperimentStatus.STARTING
            and manifest.parameter_version != int(params_snapshot["version"])
        ):
            raise InvalidExperimentStateError(
                "The prepared experiment parameter version no longer matches the "
                "saved parameter draft. End this experiment and create a new one."
            )
        experiment = self.experiments.start(
            run_id,
            params_snapshot=params_snapshot,
            parameter_version=int(params_snapshot["version"]),
        )
        # STARTING is intentionally persisted before RabbitMQ delivery. If a
        # publish is interrupted, the immutable snapshot can be retried while
        # configuration changes and experiment switching remain locked.
        self.operation_state["experiment_active"] = True
        try:
            parameter_messages = self.publisher.publish_params(
                params_snapshot["shared"],
                params_snapshot["gsensor"],
                params_snapshot["controller"],
                int(params_snapshot["version"]),
            )
            commands = self.publisher.publish_experiment_start_command(
                experiment,
                adaptation_enabled=self.run_configuration.adaptation_enabled,
                adaptation_mode=self.run_configuration.adaptation_mode,
            )
        except Exception as exc:
            raise RuntimeError(
                "The experiment snapshot was saved, but service startup delivery did "
                "not complete. Keep this experiment selected and retry Start."
            ) from exc
        return {
            "triggered": True,
            "key": "experiment_active",
            "value": True,
            "parameter_messages": parameter_messages,
            "parameters": self.params_payload(),
            "commands": commands,
            "experiment": experiment,
        }

    def finish_experiment(self, run_id: str) -> Dict[str, Any]:
        manifest = self.experiments.registry.get(run_id)
        current_run_id = self.experiments.current_run_id()

        if manifest.status in {ExperimentStatus.COMPLETED, ExperimentStatus.ERROR}:
            experiment = self.experiments.get(run_id)
        elif manifest.status == ExperimentStatus.CREATED:
            experiment = self.experiments.finish(run_id)
        else:
            if run_id != current_run_id:
                raise InvalidExperimentStateError(
                    "Only the current experiment can be ended while it is active."
                )
            retrying = manifest.status == ExperimentStatus.STOPPING
            experiment = (
                self.experiments.get(run_id)
                if retrying
                else self.experiments.request_stop(run_id)
            )
            # Persist STOPPING before delivery. A broker interruption can then
            # be recovered by pressing Retry End for this same run.
            self.operation_state["experiment_active"] = False
            try:
                self.publisher.publish_experiment_stop_command(
                    run_id,
                    reason=(
                        "central_retry_end_experiment"
                        if retrying
                        else "central_end_experiment"
                    ),
                )
            except Exception as exc:
                raise RuntimeError(
                    "The experiment is marked as stopping, but service stop delivery "
                    "did not complete. Keep this experiment selected and retry End."
                ) from exc

        if run_id == current_run_id:
            self.operation_state["experiment_active"] = False
        return experiment

    def add_seed(self) -> Dict[str, Any]:
        """Record one operator-confirmed seed addition in the live Controller."""

        run_id, _controller = self._require_running_controller("Add Seed")
        command = self.publisher.publish_controller_add_seed_command(run_id)
        return {
            "requested": True,
            "event": dict(command["payload"]),
            "command": command,
        }

    def set_adaptation(self, enabled: bool) -> Dict[str, Any]:
        """Start or stop Controller parameter adaptation for the current run."""

        run_id, controller = self._require_running_controller("Adaptation")
        current_enabled = bool(controller.get("adaptation", {}).get("enabled", False))
        if current_enabled == enabled:
            return {
                "requested": False,
                "unchanged": True,
                "adaptation": dict(controller.get("adaptation", {})),
            }

        command = self.publisher.publish_controller_adaptation_command(
            run_id,
            enabled=enabled,
            mode=self.run_configuration.adaptation_mode,
        )
        return {
            "requested": True,
            "event": dict(command["payload"]),
            "command": command,
        }

    def _require_running_controller(
        self,
        action_label: str,
    ) -> tuple[str, Dict[str, Any]]:
        run_id = self.experiments.current_run_id()
        if run_id is None:
            raise InvalidExperimentStateError(
                f"Create and start an experiment before using {action_label}."
            )
        manifest = self.experiments.registry.get(run_id)
        active_statuses = {
            ExperimentStatus.STARTING,
            ExperimentStatus.WAITING_FOR_INITIAL_IMAGE,
            ExperimentStatus.INITIALIZING,
            ExperimentStatus.MEASURING,
        }
        if manifest.status not in active_statuses:
            raise InvalidExperimentStateError(
                f"{action_label} is available only while an experiment is running."
            )

        controller = self.controller_status()
        if not controller.get("available"):
            raise RuntimeError(
                f"Controller is unavailable; {action_label} was not sent."
            )
        if controller.get("status") != "running":
            raise InvalidExperimentStateError(
                f"Controller is not ready for {action_label}."
            )
        if controller.get("current_run_id") != run_id:
            raise InvalidExperimentStateError(
                "Controller run_id does not match the current Central experiment."
            )
        return run_id, controller


def _retain_last_gsensor_frame(
    previous: Dict[str, Any] | None,
    current: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep the latest frame reference when a terminal status omits it."""

    merged = dict(current)
    if previous is None or previous.get("run_id") != merged.get("run_id"):
        return merged
    if merged.get("frame_seq") is None:
        merged["frame_seq"] = previous.get("frame_seq")
    if not merged.get("image_name"):
        merged["image_name"] = previous.get("image_name")
    return merged


service = CentralService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    try:
        yield
    finally:
        service.stop()


web_app = FastAPI(title="Crystallization MPC Central UI", lifespan=lifespan)
web_app.mount("/static", StaticFiles(directory=UI_DIR / "static"), name="static")


@web_app.middleware("http")
async def no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@web_app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (UI_DIR / "static" / "index.html").read_text(encoding="utf-8")


@web_app.get("/api/ui/config")
def get_ui_config() -> Dict[str, str | bool]:
    return service.ui_config()


@web_app.get("/api/params")
def get_params() -> Dict[str, Any]:
    return service.params_payload()


@web_app.get("/api/run-configuration")
def get_run_configuration() -> Dict[str, Any]:
    try:
        return service.run_configuration_payload()
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.put("/api/run-configuration")
def update_run_configuration(payload: RunConfigurationUpdate) -> Dict[str, Any]:
    try:
        return service.update_run_configuration(payload)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/params")
def update_params(payload: ParamsUpdate) -> Dict[str, Any]:
    try:
        return service.save_params(payload)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/params/reset")
def reset_params() -> Dict[str, Any]:
    try:
        return service.reset_params_to_defaults()
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/experiments", status_code=201)
def create_experiment(payload: ExperimentCreateRequest) -> Dict[str, Any]:
    try:
        return service.create_experiment(label=payload.label)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/experiments")
def list_experiments() -> Dict[str, Any]:
    try:
        return service.experiments.list()
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/experiments/{run_id}")
def get_experiment(run_id: str) -> Dict[str, Any]:
    try:
        return service.experiments.get(run_id)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/experiments/{run_id}/overlay/{kind}")
def get_experiment_overlay(run_id: str, kind: str) -> FileResponse:
    try:
        path = service.overlay_path(run_id, kind)
        return FileResponse(path, media_type="image/jpeg", filename=path.name)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/experiments/{run_id}/select")
def select_experiment(run_id: str) -> Dict[str, Any]:
    try:
        return service.select_experiment(run_id)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/experiments/{run_id}/finish")
def finish_experiment(run_id: str) -> Dict[str, Any]:
    try:
        return service.finish_experiment(run_id)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/operation/state")
def get_operation_state() -> Dict[str, Any]:
    preview = service.preview_publish_payload()
    return {
        "target": service.target,
        "state": service.operation_state,
        "run_configuration": service.run_configuration_payload(),
        "preview": preview,
    }


@web_app.get("/api/system/status")
def get_system_status() -> Dict[str, Any]:
    try:
        return service.system_status()
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/operation/meta")
def get_operation_meta() -> Dict[str, Any]:
    return {
        "sections": service.load_operation_meta(),
    }


@web_app.post("/api/operation/target")
def update_target(payload: TargetUpdate) -> Dict[str, Any]:
    try:
        updated = service.run_configuration.to_dict()
        updated["control_target"] = payload.target
        result = service.update_run_configuration(updated)
        return {
            "saved": True,
            "target": service.target,
            "run_configuration": result,
        }
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/operation/value")
def update_operation_value(payload: OperationValueUpdate) -> Dict[str, Any]:
    try:
        return service.update_operation_value(payload.key, payload.value)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/operation/experiment/start")
def start_experiment(payload: Optional[ParamsUpdate] = None) -> Dict[str, Any]:
    try:
        return service.start_experiment(payload)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/operation/controller/add-seed")
def add_seed() -> Dict[str, Any]:
    try:
        return service.add_seed()
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/operation/controller/adaptation")
def set_adaptation(payload: AdaptationUpdate) -> Dict[str, Any]:
    try:
        return service.set_adaptation(payload.enabled)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


def _experiment_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, ExperimentNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, InvalidExperimentIdentifierError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, InvalidExperimentStateError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ParameterValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ExperimentRegistryError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


__all__ = ["CentralApp", "web_app"]
