from __future__ import annotations

import logging
import math
import copy
import hashlib
import os
import json
import stat
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from crystallization_mpc.apps.ui_mode import resolve_ui_mode, ui_mode_payload
from crystallization_mpc.apps.central.experiments import CentralExperimentManager
from crystallization_mpc.apps.central.archive import ExperimentArchive
from crystallization_mpc.infra.storage_paths import system_path
from crystallization_mpc.infra.file_version import file_version
from crystallization_mpc.infra.runtime_history import RuntimeHistoryEventError
from crystallization_mpc.apps.central.runtime_controls import RuntimeRequestStore
from crystallization_mpc.apps.central.delivery import DeliveryLane, DeliveryBusy, DeliveryCancelled
from crystallization_mpc.apps.central.params import (
    ParameterValidationError,
    apply_derived_params,
    load_operation_meta,
    load_param_meta,
    load_params,
    load_params_document,
    load_runtime_params,
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
    ExperimentManifest,
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
    CONTROLLER_RUNTIME_UPDATE_COMMAND,
    EXPERIMENT_MODE_LIVE,
    EXPERIMENT_SELECT_COMMAND,
    EXPERIMENT_START_COMMAND,
    EXPERIMENT_STOP_COMMAND,
    GSENSOR_DISABLE_COMMAND,
    GSENSOR_ENABLE_COMMAND,
    GROWTH_RATE_COMPLETED_MESSAGE,
    GROWTH_RATE_STATUS_MESSAGE,
    PARAMS_UPDATE_MESSAGE,
)
from crystallization_mpc.messaging.contracts import (
    ControllerAddSeedPayload,
    ExperimentStartPayload,
    ExperimentStopPayload,
    GsensorActivationPayload,
    GrowthRateStatus,
    GrowthRateStatusPayload,
)
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for, route
from crystallization_mpc.messaging.schema import build_envelope, utc_ts
from crystallization_mpc.messaging.controller_runtime import ControllerRuntimeUpdatePayload
from crystallization_mpc.messaging.run_parameters import RunParametersUpdatePayload
from crystallization_mpc.messaging.parameter_policy import (
    derived_parameter_policies, parameter_policies, validate_parameter_changes,
)
from crystallization_mpc.apps.central.process_source import ProcessSourceMonitor

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
GSENSOR_ACTIVATION_STATE_FILENAME = ".central_gsensor_activation.json"
GSENSOR_PROCESSING_STATE_FILENAME = "gsensor_processing_state.json"
CONTROLLER_OBSERVATION_MAX_AGE_SEC = 5.0
logger = logging.getLogger(__name__)


class ControllerUnavailableError(RuntimeError):
    """No fresh Controller observation authorizes this operation."""


class RuntimeRequestNotSent(RuntimeError):
    """A previously unknown event failed preflight, before persistence/delivery."""

    def __init__(self, event, cause):
        super().__init__(str(cause))
        self.event = event
        self.cause = cause


class ExperimentContext(BaseModel):
    expected_run_id: str | None = None


class TargetUpdate(ExperimentContext):
    target: Literal["sigma", "G"]


class ParamsUpdate(ExperimentContext):
    version: int = 1
    shared: Dict[str, Any] = Field(default_factory=dict)
    gsensor: Dict[str, Any] = Field(default_factory=dict)
    controller: Dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    experiment_started_at: str | None = None
    requested_at: str | None = None
    changes: Dict[str, Dict[str, Any]] | None = None


class RunConfigurationUpdate(ExperimentContext):
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

class OperationValueUpdate(ExperimentContext):
    key: str
    value: Any


class ControllerRuntimeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    event_id: str
    expected_revision: int
    changes: Dict[str, Any]
    requested_at: str


class GsensorActivationRequest(BaseModel):
    """One optimistic-concurrency request from the Central UI."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    enabled: bool
    expected_revision: int = Field(ge=0)
    event_id: str

class ExperimentCreateRequest(ExperimentContext):
    label: str | None = Field(default=None, max_length=120)


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
        self._delivery_guard = None

    def independent(self):
        return CentralApp(self.url, self.exchange, self.queue_name, self.include_broadcast)

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
            if self._delivery_guard:
                self._delivery_guard()
            try:
                ch = self._require_channel()
                if self._delivery_guard:
                    self._delivery_guard()
                publish(ch, self.exchange, routing_key, payload, persistent=persistent)
                return
            except DeliveryCancelled:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "RabbitMQ publish failed on attempt %s/2, reconnecting.",
                    attempt + 1,
                    exc_info=True,
                )
                self.close()
        raise RuntimeError("RabbitMQ publish failed after reconnect.") from last_error

    def publish_params(
        self,
        shared: Dict[str, object],
        gsensor: Dict[str, object],
        controller: Dict[str, object],
        version: int,
        *,
        parameter_sections: Dict[str, Dict[str, object]] | None = None,
        include_parameter_sections: bool = True,
        experiment: Dict[str, Any] | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        seq = next_seq()
        messages: Dict[str, Dict[str, Any]] = {}
        identity = ({"run_id": experiment["run_id"], "experiment_started_at": experiment["started_at"]}
                    if experiment is not None else {})

        if shared or gsensor:
            payload = {"version": version, "params": {**shared, **gsensor}, **identity}
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
            sections = parameter_sections or {"shared": shared, "gsensor": gsensor, "controller": controller}
            policies = parameter_policies()
            payload = {"version": version, "params": {**shared, **controller}, **identity}
            if include_parameter_sections:
                payload["parameter_sections"] = {
                    section: {key: value for key, value in values.items() if key in policies[section]}
                    for section, values in sections.items()}
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
        experiment_started_at: str | None = None,
        seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = ExperimentStopPayload(
            run_id=run_id,
            stopped_at=stopped_at,
            reason=reason,
        )
        fields = payload.to_dict()
        if experiment_started_at is not None:
            if not isinstance(experiment_started_at, str) or not experiment_started_at.strip():
                raise ValueError("experiment_started_at must be a nonempty registry timestamp.")
            # Extra identity evidence is backward compatible with the ordinary
            # stop contract. Recovery-held Controller runs require this field.
            fields["experiment_started_at"] = experiment_started_at
        return build_envelope(
            src=ROLE,
            dst=dst,
            msg_type="command",
            name=EXPERIMENT_STOP_COMMAND,
            seq=next_seq() if seq is None else seq,
            payload=fields,
        )

    def publish_experiment_stop_command(
        self,
        run_id: str,
        *,
        reason: str = "central_stop",
        experiment_started_at: str | None = None,
        controller_only: bool = False,
        gsensor_only: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        if controller_only and gsensor_only:
            raise ValueError("Stop may target one participant or both, not conflicting exclusive targets.")
        seq = next_seq()
        stopped_at = utc_ts()
        messages: Dict[str, Dict[str, Any]] = {}
        destinations = ("controller",) if controller_only else (("gsensor",) if gsensor_only else ("gsensor", "controller"))
        for dst in destinations:
            env = self.build_experiment_stop_command(
                run_id,
                dst=dst,
                stopped_at=stopped_at,
                reason=reason,
                experiment_started_at=experiment_started_at,
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

    def publish_controller_runtime_command(
        self, payload: ControllerRuntimeUpdatePayload,
    ) -> Dict[str, Any]:
        env = build_envelope(
            src=ROLE, dst="controller", msg_type="command",
            name=CONTROLLER_RUNTIME_UPDATE_COMMAND, seq=next_seq(),
            payload=payload.to_dict(),
        )
        self._publish_with_reconnect(route(ROLE, "controller"), env, persistent=True)
        return env

    def publish_run_parameters_command(self, payload: RunParametersUpdatePayload) -> Dict[str, Any]:
        env = build_envelope(
            src=ROLE, dst="controller", msg_type="command",
            name="experiment.parameters.update", seq=next_seq(), payload=payload.to_dict(),
        )
        self._publish_with_reconnect(route(ROLE, "controller"), env, persistent=True)
        return env

    def build_gsensor_activation_command(
        self,
        payload: GsensorActivationPayload,
        *,
        enabled: bool,
        dst: str,
        seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return build_envelope(
            src=ROLE,
            dst=dst,
            msg_type="command",
            name=(GSENSOR_ENABLE_COMMAND if enabled else GSENSOR_DISABLE_COMMAND),
            seq=next_seq() if seq is None else seq,
            payload=payload.to_dict(),
        )

    def publish_gsensor_activation_command(
        self,
        payload: GsensorActivationPayload,
        *,
        enabled: bool,
    ) -> Dict[str, Dict[str, Any]]:
        """Close/open Controller's G gate before changing the Gsensor producer."""

        seq = next_seq()
        messages: Dict[str, Dict[str, Any]] = {}
        for dst in ("controller", "gsensor"):
            envelope = self.build_gsensor_activation_command(
                payload,
                enabled=enabled,
                dst=dst,
                seq=seq,
            )
            self._publish_with_reconnect(
                route(ROLE, dst),
                envelope,
                persistent=True,
            )
            messages[dst] = envelope
        return messages

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

def _action_lock(name, *, lifecycle=False, timeout=0.1):
    """Serialize one actual conflict domain; never use the snapshot lock for I/O."""
    def decorate(method):
        @wraps(method)
        def call(self, *args, **kwargs):
            lock = getattr(self, name)
            if not lock.acquire(timeout=timeout):
                raise DeliveryBusy("A related operation is still preparing; retry the same request.")
            try:
                self._check_ui_context()
                try:
                    return method(self, *args, **kwargs)
                finally:
                    if lifecycle:
                        self._publish_local_status()
            finally:
                lock.release()
        return call
    return decorate


class CentralService:
    def __init__(self, publisher: CentralApp | None = None, *, stop_publisher: CentralApp | None = None) -> None:
        self.ui_mode = resolve_ui_mode()
        self.default_params_path = Path(os.getenv("PARAMS_DEFAULT_FILE", str(DEFAULT_PARAMS_PATH)))
        self.param_meta_path = Path(os.getenv("PARAM_META_FILE", str(DEFAULT_PARAM_META_PATH)))
        self.operation_meta_path = Path(os.getenv("OPERATION_META_FILE", str(DEFAULT_OPERATION_META_PATH)))
        self.experiment_root = Path(
            os.getenv("EXPERIMENT_ROOT", str(DEFAULT_EXPERIMENT_ROOT))
        )
        self.params_path = system_path(
            self.experiment_root, "params_runtime.yaml", explicit_path=os.getenv("PARAMS_FILE")
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
        run_configuration_path = system_path(
            self.experiment_root, RUN_CONFIGURATION_FILENAME,
            explicit_path=os.getenv("RUN_CONFIGURATION_FILE"),
        )
        self.run_configuration_store = RunConfigurationStore(run_configuration_path)
        self.run_configuration = self.run_configuration_store.load(
            self.default_run_configuration
        )
        self.target: Literal["sigma", "G"] = self.run_configuration.control_target  # type: ignore[assignment]
        self.publisher = publisher or CentralApp()
        self.stop_publisher = stop_publisher or (
            self.publisher.independent() if isinstance(self.publisher, CentralApp) else CentralApp()
        )
        if self.stop_publisher is self.publisher:
            raise ValueError("Stop delivery requires an independent publisher connection.")
        self._ordinary_delivery = DeliveryLane(self.publisher, "central-command-delivery")
        self._stop_delivery = DeliveryLane(self.stop_publisher, "central-stop-delivery", capacity=8)
        self.process_source = ProcessSourceMonitor.from_env(
            getattr(self.publisher, "url", os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")),
            getattr(self.publisher, "exchange", os.getenv("RABBIT_EXCHANGE", EXCHANGE)),
        )
        self.operation_state = self._build_default_operation_state()
        self._sync_operation_state_from_run_configuration()
        self.controller_status_url = os.getenv(
            "CONTROLLER_STATUS_URL", "http://localhost:8002/api/status"
        )
        self.gsensor_status_url = os.getenv(
            "GSENSOR_STATUS_URL", "http://localhost:8001/api/status"
        )
        self.last_gsensor_status: Dict[str, Any] | None = None
        self.last_gsensor_status_received_at: str | None = None
        self.last_status_consumer_error: str | None = None
        self.last_rejected_status_error: str | None = None
        self.status_message_count = 0
        self.status_rejection_count = 0
        self._consumer_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._preparation_lock = threading.RLock()
        self._operation_cache_lock = threading.RLock()
        self._operation_cache = None
        self._runtime_action_lock = threading.RLock()
        self._activation_lock = threading.RLock()
        self._message_lock = threading.RLock()
        self._end_lock = threading.RLock()
        self._ui_context = threading.local()
        self._status_stop = threading.Event()
        self._status_thread = None
        self.controller_observation_max_age_sec = float(os.getenv(
            "CENTRAL_CONTROLLER_STATUS_MAX_AGE_SEC", str(CONTROLLER_OBSERVATION_MAX_AGE_SEC)
        ))
        if not math.isfinite(self.controller_observation_max_age_sec) or self.controller_observation_max_age_sec <= 0:
            raise ValueError("CENTRAL_CONTROLLER_STATUS_MAX_AGE_SEC must be finite and positive.")
        self._controller_poll_stop = threading.Event()
        self._controller_poll_thread = None
        self._controller_poll_lock = threading.Lock()
        self._status_generation = 0
        self._stopping_runs = set()
        self._controller_observation = {"available": False, "status": "unavailable", "active": False,
                                        "error": "Waiting for Controller status.", "observed_at": None}
        self._controller_observed_monotonic = None
        self._status_snapshot = {"observed_at": None, "controller": copy.deepcopy(self._controller_observation),
                                 "runtime_request": None, "runtime_history": [], "runtime_history_error": None,
                                 "archive": None, "archive_error": None,
                                 "process_source": self.process_source.status()}
        self.runtime_request_store = RuntimeRequestStore(self.experiment_root)
        # History requests own their coordination; their network/storage work
        # never holds the live status snapshot lock.
        self._runtime_history_sync_lock = threading.RLock()
        self._runtime_history_cursors: dict[str, int] = {}
        self.runtime_history_error: str | None = None
        self._runtime_monitor_stop = threading.Event()
        self._runtime_monitor_thread: threading.Thread | None = None
        self.runtime_export_error: str | None = None
        self.runtime_export_enabled = os.getenv("CENTRAL_RUNTIME_INFLUX_ENABLED", os.getenv("CONTROLLER_INFLUX_ENABLED", "false")).lower() == "true"
        self.archive = ExperimentArchive(self.experiments, controller_status=self.controller_status)
        self._publish_local_status()

    def _sync_runtime_history(self, run_id: str, controller: dict[str, Any], *, all_pages=False):
        # Serialize cursor advancement with explicit history refreshes without
        # taking the service lock. RuntimeHistory protects its own atomic saves.
        with self._runtime_history_sync_lock:
            self.runtime_history_error = None
            if not (controller.get("available") and (controller.get("runtime_controls") or {}).get("history_supported")):
                return
            try:
                cursor = 0 if all_pages else self._runtime_history_cursors.get(run_id, 0)
                while True:
                    query = urllib.parse.urlencode({"run_id": run_id, "after": cursor, "limit": 100})
                    url = self.controller_status_url.rsplit("/", 1)[0] + "/runtime-history?" + query
                    with urllib.request.urlopen(url, timeout=2) as response:
                        page = json.load(response)
                    if page.get("error"):
                        raise ValueError(page["error"])
                    previous_cursor = cursor
                    for entry in page["entries"]:
                        if entry["command"]["run_id"] != run_id:
                            raise ValueError("History run mismatch.")
                        sequence = entry["sequence"]
                        if type(sequence) is not int or sequence <= cursor:
                            raise ValueError("History sequence must advance.")
                        # Central assigns its own ordering, since it also has pending requests.
                        self.runtime_request_store.history.save({k: v for k, v in entry.items()
                                                                if k not in {"sequence", "grafana_sync", "grafana_error"}})
                        cursor = sequence
                        self._runtime_history_cursors[run_id] = max(
                            sequence, self._runtime_history_cursors.get(run_id, 0)
                        )
                    self.runtime_history_error = None
                    if not all_pages or page.get("next_cursor") is None:
                        break
                    if cursor == previous_cursor:
                        raise ValueError("History pagination did not advance.")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                detail = str(exc) if isinstance(exc, RuntimeHistoryEventError) else type(exc).__name__
                self.runtime_history_error = f"History synchronization incomplete: {detail}"

    def runtime_history_page(self, run_id: str, before: int | None = None, limit: int = 50):
        self.experiments.registry.get(run_id)
        self._sync_runtime_history(run_id, self.controller_status(), all_pages=True)
        try:
            page = self.runtime_request_store.history.page(run_id, before=before, limit=limit)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            page = {"entries": [], "next_cursor": None}
            detail = str(exc) if isinstance(exc, RuntimeHistoryEventError) else type(exc).__name__
            self.runtime_history_error = f"History unavailable: {detail}"
        return {**page,
                "error": self.runtime_history_error, "grafana_enabled": self.runtime_export_enabled,
                "grafana_error": self.runtime_export_error}

    def _runtime_monitor(self):
        from crystallization_mpc.apps.central.runtime_telemetry import export_pending, create_runtime_writer
        writer = None
        try:
            while not self._runtime_monitor_stop.is_set():
                try:
                    run_id = self.experiments.current_run_id()
                    controller = self.controller_status()
                    if run_id is not None:
                        self._sync_runtime_history(run_id, controller)
                    if self.runtime_export_enabled:
                        if writer is None:
                            writer = create_runtime_writer(self.experiment_root)
                        export_pending(self.runtime_request_store.history, writer)
                    self.runtime_export_error = None
                except Exception as exc:
                    self.runtime_export_error = f"Runtime event synchronization incomplete: {type(exc).__name__}"
                self._runtime_monitor_stop.wait(2)
        finally:
            if writer is not None:
                writer.close()

    def _active_params_path(self) -> Path:
        if self.params_path.exists():
            return self.params_path
        return self.default_params_path

    def ui_config(self) -> Dict[str, Any]:
        return {**ui_mode_payload(self.ui_mode), "service_links": {
            name: os.getenv(f"CENTRAL_{name.upper()}_UI_URL", "").strip()
            for name in ("grafana", "gsensor", "rabbitmq", "influxdb")
        }}

    @contextmanager
    def ui_action(self, context: ExperimentContext | None = None):
        """Carry displayed identity to the operation's own commit/prepare lock."""
        previous = getattr(self._ui_context, "value", None)
        self._ui_context.value = context
        try:
            yield
        finally:
            self._ui_context.value = previous

    def _check_ui_context(self):
        context = getattr(self._ui_context, "value", None)
        if context is not None and "expected_run_id" in context.model_fields_set:
            if context.expected_run_id != self.experiments.current_run_id():
                raise InvalidExperimentStateError("The selected experiment changed. Refresh before retrying.")

    def _deliver(self, key, run_id, action, *, stop=False, completed=None):
        lane = self._stop_delivery if stop else self._ordinary_delivery
        def deliver(publisher):
            if not stop and not key.startswith("select:"):
                self._require_run_open(run_id)
            return action(publisher)
        job = lane.submit(key, run_id, deliver, completed)
        delivery, result = lane.wait(job)
        return delivery, result

    def _require_run_open(self, run_id):
        with self._lock:
            ending = run_id in self._stopping_runs
        manifest = self.experiments.registry.get(run_id)
        if ending or manifest.status in {ExperimentStatus.STOPPING, ExperimentStatus.COMPLETED, ExperimentStatus.ERROR}:
            raise DeliveryCancelled("Experiment has ended; this command was not delivered.")

    def _deliver_runtime(self, event, method):
        def completed(job):
            self.runtime_request_store.update_delivery(event.run_id, event.event_id, job.public())
        delivery, _ = self._deliver(f"runtime:{event.run_id}:{event.event_id}", event.run_id,
                                    lambda p: getattr(p, method)(event), completed=completed)
        entry = self.runtime_request_store.get(event.run_id)
        if entry and entry["command"]["event_id"] == event.event_id:
            entry["delivery"] = delivery
        return entry

    @staticmethod
    def _require_delivery(delivery):
        if delivery["status"] == "cancelled":
            raise DeliveryCancelled(delivery["error"] or "Experiment ended before delivery.")
        if delivery["status"] == "unconfirmed":
            raise RuntimeError(delivery["error"] or "Command delivery is unconfirmed.")

    def _publish_local_status(self):
        # Registry/activation files are never read while holding the snapshot lock.
        with self._lock:
            self._status_generation += 1
            generation = self._status_generation
        try:
            experiments = self.experiments.list()
            current = next((x for x in experiments["experiments"]
                            if x["run_id"] == experiments["current_run_id"]), None)
            with self._lock:
                if generation != self._status_generation:
                    return
                gsensor = dict(self._status_snapshot.get("gsensor") or {})
                same_run = self.last_gsensor_status and self.last_gsensor_status.get("run_id") == experiments["current_run_id"]
                gsensor.update(last_status=copy.deepcopy(self.last_gsensor_status) if same_run else None,
                    received_at=self.last_gsensor_status_received_at if same_run else None,
                    consumer_error=self.last_status_consumer_error, last_rejection_error=self.last_rejected_status_error,
                    message_count=self.status_message_count, rejection_count=self.status_rejection_count)
                if (gsensor.get("activation") or {}).get("run_id") != experiments["current_run_id"]:
                    gsensor["activation"] = None
                self._status_snapshot = {**self._status_snapshot, "experiments": experiments,
                                         "current_experiment": current, "gsensor": gsensor}
        except Exception:
            logger.exception("Could not publish local experiment status.")

    def _status_monitor(self):
        while not self._status_stop.is_set():
            try:
                self.refresh_status()
            except Exception:
                logger.exception("Central status refresh failed; retaining last complete snapshot.")
            # AMQP bursts must not increase the remote polling rate.
            self._status_stop.wait(1)

    def _controller_monitor(self):
        # This lane never enters the experiment registry, activation, history or
        # archive locks. Slow host mounts must not age an online observation.
        while not self._controller_poll_stop.is_set():
            try:
                self._refresh_controller_observation()
            except Exception:
                logger.exception("Controller observation failed; waiting for the next poll.")
            self._controller_poll_stop.wait(1)

    def _refresh_controller_observation(self):
        # Serialize on-demand and background reads, bounding the wait for either.
        # No status/snapshot lock is held during the HTTP request.
        if not self._controller_poll_lock.acquire(timeout=1.0):
            return self.controller_status()
        try:
            controller = self._fetch_controller_status()
            controller["observed_at"] = utc_ts()
            observed = time.monotonic()
            with self._lock:
                self._controller_observation = controller
                self._controller_observed_monotonic = observed
        finally:
            self._controller_poll_lock.release()
        return self.controller_status()

    def refresh_status(self):
        with self._lock:
            generation = self._status_generation
        controller = self.controller_status()
        snapshot = self._collect_system_status(controller)
        with self._lock:
            if generation != self._status_generation:
                # Messages/actions can advance local lifecycle during HTTP or
                # storage waits. Publish the observation without rewinding it.
                latest = self._status_snapshot
                previous_run = (snapshot.get("current_experiment") or {}).get("run_id")
                current_run = (latest.get("current_experiment") or {}).get("run_id")
                snapshot.update(experiments=latest["experiments"], current_experiment=latest["current_experiment"])
                activation = snapshot["gsensor"].get("activation")
                snapshot["gsensor"] = copy.deepcopy(latest["gsensor"])
                if previous_run == current_run:
                    snapshot["gsensor"]["activation"] = activation
                else:
                    snapshot.update(runtime_request=None, runtime_history=[], archive=None, archive_error=None)
            self._status_snapshot = snapshot

    def system_status(self):
        with self._lock:
            payload = copy.deepcopy(self._status_snapshot)
        # The optional details snapshot may be delayed by filesystem work. Read
        # Controller from its independent observation, never that old snapshot.
        payload["controller"] = self.controller_status()
        payload["delivery"] = {"ordinary": self._ordinary_delivery.status(), "stop": self._stop_delivery.status()}
        return payload

    def _gsensor_activation_path(
        self, run_id: str, *, run_directory: Path | None = None,
    ) -> Path:
        if run_directory is None:
            manifest = self.experiments.registry.get(run_id)
            run_directory = self.experiments.registry.run_dir(manifest.run_id)
        else:
            # Only internal callers pass a directory from a validated registry
            # listing. Recheck containment without repeating that whole listing.
            run_directory = Path(run_directory)
            directory_info = run_directory.lstat()
            if (stat.S_ISLNK(directory_info.st_mode)
                    or getattr(directory_info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise InvalidExperimentIdentifierError(
                    "Gsensor activation state directory cannot be a link."
                )
            run_directory = run_directory.resolve()
        root = self.experiment_root.resolve()
        try:
            run_directory.relative_to(root)
            if run_directory == root:
                raise ValueError("Experiment directory cannot be the root.")
        except ValueError as exc:
            raise InvalidExperimentIdentifierError(
                "Gsensor activation state path escapes the experiment root."
            ) from exc
        return run_directory / GSENSOR_ACTIVATION_STATE_FILENAME

    @staticmethod
    def _new_gsensor_activation_state(manifest) -> Dict[str, Any]:
        if manifest.started_at is None:
            raise InvalidExperimentStateError(
                "Start the experiment before changing Gsensor activation."
            )
        return {
            "schema_version": 1,
            "run_id": manifest.run_id,
            "experiment_started_at": manifest.started_at,
            # `enabled` is confirmed by Gsensor status. Publishing a command never
            # changes it optimistically.
            "enabled": False,
            # `revision` is the latest revision issued or observed and is the UI's
            # optimistic-concurrency token.
            "revision": 0,
            "acknowledged_revision": 0,
            "acknowledged_event_id": None,
            "acknowledged_at": None,
            "pending": None,
            "last_request": None,
            "last_error": None,
            "updated_at": utc_ts(),
        }

    def _load_gsensor_activation_state(
        self,
        manifest,
        *,
        create: bool,
        run_directory: Path | None = None,
    ) -> Dict[str, Any]:
        path = self._gsensor_activation_path(manifest.run_id, run_directory=run_directory)
        if not path.exists():
            state = self._new_gsensor_activation_state(manifest)
            if create:
                self._save_gsensor_activation_state(state)
            return state
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not read Gsensor activation state: {path}"
            ) from exc
        self._validate_gsensor_activation_state(state, manifest)
        return state

    @staticmethod
    def _validate_gsensor_activation_state(state: Any, manifest) -> None:
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("Invalid Central Gsensor activation state.")
        if state.get("run_id") != manifest.run_id:
            raise ValueError("Central Gsensor activation run_id mismatch.")
        if (
            not isinstance(state.get("experiment_started_at"), str)
            or manifest.started_at is None
            or not _same_timestamp(
                state["experiment_started_at"],
                manifest.started_at,
            )
        ):
            raise ValueError("Central Gsensor activation experiment start mismatch.")
        if not isinstance(state.get("enabled"), bool):
            raise ValueError("Central Gsensor activation enabled state is invalid.")
        revision = state.get("revision")
        acknowledged_revision = state.get("acknowledged_revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("Central Gsensor activation revision is invalid.")
        if (
            type(acknowledged_revision) is not int
            or acknowledged_revision < 0
            or acknowledged_revision > revision
        ):
            raise ValueError(
                "Central Gsensor activation acknowledged revision is invalid."
            )
        acknowledged_event_id = state.get("acknowledged_event_id")
        if acknowledged_event_id is not None and (
            not isinstance(acknowledged_event_id, str)
            or not acknowledged_event_id.strip()
        ):
            raise ValueError(
                "Central Gsensor activation acknowledged event_id is invalid."
            )
        acknowledged_at = state.get("acknowledged_at")
        if acknowledged_at is not None and (
            not isinstance(acknowledged_at, str) or not acknowledged_at.strip()
        ):
            raise ValueError(
                "Central Gsensor activation acknowledgment timestamp is invalid."
            )
        pending = state.get("pending")
        if pending is not None:
            if not isinstance(pending, dict):
                raise ValueError("Central Gsensor activation pending state is invalid.")
            command = GsensorActivationPayload.from_mapping(pending)
            if command.run_id != manifest.run_id:
                raise ValueError("Pending Gsensor activation run_id mismatch.")
            if not _same_timestamp(
                command.experiment_started_at,
                manifest.started_at,
            ):
                raise ValueError("Pending Gsensor activation start mismatch.")
            if command.revision != revision:
                raise ValueError("Pending Gsensor activation revision mismatch.")
            if not isinstance(pending.get("enabled"), bool):
                raise ValueError("Pending Gsensor activation enabled state is invalid.")
            expected_revision = pending.get("expected_revision")
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError(
                    "Pending Gsensor activation expected revision is invalid."
                )
            transport_error = pending.get("transport_error")
            if transport_error is not None and not isinstance(transport_error, str):
                raise ValueError("Pending Gsensor activation transport error is invalid.")
        last_request = state.get("last_request")
        if last_request is not None:
            if not isinstance(last_request, dict):
                raise ValueError("Central Gsensor activation last request is invalid.")
            GsensorActivationPayload.from_mapping(last_request)
            if not isinstance(last_request.get("enabled"), bool):
                raise ValueError(
                    "Central Gsensor activation last request enabled state is invalid."
                )
            expected_revision = last_request.get("expected_revision")
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError(
                    "Central Gsensor activation last request revision is invalid."
                )
        last_error = state.get("last_error")
        if last_error is not None and not isinstance(last_error, str):
            raise ValueError("Central Gsensor activation last error is invalid.")

    def _save_gsensor_activation_state(self, state: Dict[str, Any], *, manifest=None,
                                       run_directory: Path | None = None) -> None:
        manifest = manifest or self.experiments.registry.get(str(state.get("run_id", "")))
        if state.get("run_id") != manifest.run_id:
            raise ValueError("Central Gsensor activation run_id mismatch.")
        self._validate_gsensor_activation_state(state, manifest)
        state["updated_at"] = utc_ts()
        _atomic_write_json(self._gsensor_activation_path(manifest.run_id, run_directory=run_directory), state)

    @staticmethod
    def _gsensor_activation_allowed(manifest) -> bool:
        return manifest.started_at is not None and manifest.status in {
            ExperimentStatus.STARTING,
            ExperimentStatus.WAITING_FOR_INITIAL_IMAGE,
            ExperimentStatus.INITIALIZING,
            ExperimentStatus.MEASURING,
        }

    def _gsensor_activation_public_state(
        self,
        manifest=None,
        state: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if manifest is None:
            run_id = self.experiments.current_run_id()
            if run_id is None:
                return {
                    "run_id": None,
                    "experiment_started_at": None,
                    "enabled": False,
                    "desired_enabled": False,
                    "revision": 0,
                    "acknowledged_revision": 0,
                    "acknowledged_event_id": None,
                    "acknowledged_at": None,
                    "pending": None,
                    "last_error": None,
                    "updated_at": None,
                    "can_toggle": False,
                    "can_cancel_pending_enable": False,
                    "blocked_reason": "Create and start an experiment first.",
                }
            manifest = self.experiments.registry.get(run_id)
        if manifest.started_at is None:
            return {
                "run_id": manifest.run_id,
                "experiment_started_at": None,
                "enabled": False,
                "desired_enabled": False,
                "revision": 0,
                "acknowledged_revision": 0,
                "acknowledged_event_id": None,
                "acknowledged_at": None,
                "pending": None,
                "last_error": None,
                "updated_at": None,
                "can_toggle": False,
                "can_cancel_pending_enable": False,
                "blocked_reason": "Start the experiment before changing Gsensor activation.",
            }
        if state is None:
            state = self._load_gsensor_activation_state(manifest, create=False)
        pending = state.get("pending")
        can_toggle = self._gsensor_activation_allowed(manifest)
        blocked_reason = None
        if not can_toggle:
            blocked_reason = (
                f"Gsensor activation is unavailable while the experiment is "
                f"{manifest.status.value}."
            )
        elif pending is not None and not (
            pending.get("enabled") is True
        ):
            blocked_reason = (
                "A Gsensor activation request is awaiting confirmation; retry that "
                "same event."
            )
        return {
            "run_id": state["run_id"],
            "experiment_started_at": state["experiment_started_at"],
            "enabled": state["enabled"],
            "desired_enabled": (
                pending["enabled"] if pending is not None else state["enabled"]
            ),
            "revision": state["revision"],
            "acknowledged_revision": state["acknowledged_revision"],
            "acknowledged_event_id": state.get("acknowledged_event_id"),
            "acknowledged_at": state.get("acknowledged_at"),
            "pending": dict(pending) if pending is not None else None,
            "last_error": state.get("last_error"),
            "updated_at": state.get("updated_at"),
            "can_toggle": can_toggle,
            "can_cancel_pending_enable": bool(
                can_toggle and pending is not None and pending.get("enabled") is True
            ),
            "blocked_reason": blocked_reason,
        }

    def gsensor_activation_status(self, *, experiment: Dict[str, Any] | None = None) -> Dict[str, Any]:
        with self._activation_lock:
            try:
                if experiment is not None:
                    # system_status already resolved the current manifest and
                    # directory. Keep activation state fresh, reusing only that
                    # registry result rather than scanning for the run again.
                    manifest = ExperimentManifest.from_dict(experiment)
                    state = (self._load_gsensor_activation_state(
                        manifest, create=False,
                        run_directory=Path(experiment["experiment_directory"]),
                    ) if manifest.started_at is not None else None)
                    return self._gsensor_activation_public_state(manifest, state)
                return self._gsensor_activation_public_state()
            except (OSError, RuntimeError, ValueError) as exc:
                run_id = (experiment["run_id"] if experiment is not None
                          else self.experiments.current_run_id())
                return {
                    "run_id": run_id,
                    "experiment_started_at": None,
                    "enabled": False,
                    "desired_enabled": False,
                    "revision": 0,
                    "acknowledged_revision": 0,
                    "acknowledged_event_id": None,
                    "acknowledged_at": None,
                    "pending": None,
                    "last_error": (
                        f"Gsensor activation state is unavailable: {type(exc).__name__}"
                    ),
                    "updated_at": None,
                    "can_toggle": False,
                    "can_cancel_pending_enable": False,
                    "blocked_reason": "Repair the persisted activation state before retrying.",
                }

    @_action_lock("_activation_lock")
    def _prepare_gsensor_activation(
        self,
        request: GsensorActivationRequest | Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(request, GsensorActivationRequest):
            request = GsensorActivationRequest.model_validate(request)
        run_id = self.experiments.current_run_id()
        if run_id is None:
            raise InvalidExperimentStateError(
                "Create and start an experiment before changing Gsensor activation."
            )
        if request.run_id != run_id:
            raise InvalidExperimentStateError(
                "The selected experiment changed. Refresh before changing Gsensor activation."
            )
        if not request.event_id.strip():
            raise ValueError("event_id must be nonempty text.")
        manifest = self.experiments.registry.get(run_id)
        if not self._gsensor_activation_allowed(manifest):
            if manifest.started_at is None:
                reason = "Start the experiment before changing Gsensor activation."
            else:
                reason = (
                    f"Cannot change Gsensor activation while experiment {run_id} is "
                    f"{manifest.status.value}."
                )
            raise InvalidExperimentStateError(reason)

        state = self._load_gsensor_activation_state(manifest, create=True)
        event_id = request.event_id.strip()
        last_request = state.get("last_request")
        retrying = False
        if last_request is not None and last_request.get("event_id") == event_id:
            same_request = (
                last_request.get("run_id") == run_id
                and last_request.get("enabled") is request.enabled
                and last_request.get("expected_revision")
                == request.expected_revision
                and _same_timestamp(
                    str(last_request.get("experiment_started_at")),
                    str(manifest.started_at),
                )
            )
            if not same_request:
                raise InvalidExperimentStateError(
                    "Gsensor activation event_id was reused with different content."
                )
            pending = state.get("pending")
            if pending is None:
                return {
                    "requested": False,
                    "idempotent": True,
                    "commands": {},
                    "activation": self._gsensor_activation_public_state(
                        manifest,
                        state,
                    ),
                }
            command = GsensorActivationPayload.from_mapping(pending)
            retrying = True
        else:
            if request.expected_revision != state["revision"]:
                raise InvalidExperimentStateError(
                    "Gsensor activation revision changed. Refresh before retrying."
                )
            pending = state.get("pending")
            if pending is not None:
                cancelling_enable = (
                    pending.get("enabled") is True and request.enabled is False
                )
                if not cancelling_enable:
                    raise InvalidExperimentStateError(
                        "A Gsensor activation request is awaiting confirmation; retry "
                        "that event before making another change."
                    )
            elif request.enabled is state["enabled"]:
                return {
                    "requested": False,
                    "idempotent": True,
                    "commands": {},
                    "activation": self._gsensor_activation_public_state(
                        manifest,
                        state,
                    ),
                }

            revision = state["revision"] + 1
            command = GsensorActivationPayload.from_mapping(
                {
                    "run_id": run_id,
                    "experiment_started_at": manifest.started_at,
                    "event_id": event_id,
                    "revision": revision,
                    "requested_at": utc_ts(),
                }
            )
            command_document = {
                **command.to_dict(),
                "enabled": request.enabled,
                "expected_revision": request.expected_revision,
            }
            state["revision"] = revision
            state["last_request"] = dict(command_document)
            state["pending"] = {
                **command_document,
                "transport_error": None,
            }
            state["last_error"] = None
            # The pending intent is durable before either service can receive it.
            self._save_gsensor_activation_state(state)

        return {"manifest": manifest, "command": command, "enabled": request.enabled, "retrying": retrying}

    def set_gsensor_activation(self, request):
        prepared = self._prepare_gsensor_activation(request)
        if "command" not in prepared:
            return prepared
        manifest, command = prepared["manifest"], prepared["command"]
        run_id, enabled = manifest.run_id, prepared["enabled"]
        def completed(job):
            with self._activation_lock:
                latest = self._load_gsensor_activation_state(manifest, create=True)
                pending = latest.get("pending")
                if pending and pending.get("event_id") == command.event_id:
                    pending["transport_error"] = job.error
                    pending["delivery"] = job.public()
                    self._save_gsensor_activation_state(latest)
        delivery, messages = self._deliver(f"activation:{run_id}:{command.event_id}", run_id,
            lambda p: p.publish_gsensor_activation_command(command, enabled=enabled), completed=completed)
        with self._activation_lock:
            latest = self._load_gsensor_activation_state(manifest, create=True)
        return {
            "requested": True,
            "idempotent": prepared["retrying"],
            "commands": messages or {},
            "delivery": delivery,
            "activation": self._gsensor_activation_public_state(manifest, latest),
        }

    def _observe_gsensor_activation(
        self,
        payload: GrowthRateStatusPayload,
        manifest,
        *, run_directory: Path | None = None,
    ) -> bool:
        metadata = (
            payload.enabled,
            payload.control_revision,
            payload.experiment_started_at,
            payload.control_event_id,
        )
        if all(value is None for value in metadata):
            return True

        state = self._load_gsensor_activation_state(manifest, create=False, run_directory=run_directory)
        if payload.control_revision is not None:
            if payload.control_revision < state["revision"]:
                return False
        # Older mixed-version producers may provide only part of the optional
        # bundle. It may still drive lifecycle status, but never acknowledges a
        # Central activation request.
        if any(value is None for value in metadata):
            return True

        assert payload.control_revision is not None
        assert payload.enabled is not None
        assert payload.experiment_started_at is not None
        assert payload.control_event_id is not None
        revision = payload.control_revision
        event_id = payload.control_event_id
        pending = state.get("pending")
        prior_revision = state["revision"]
        prior_enabled = state["enabled"]

        if revision > prior_revision:
            state["revision"] = revision
            state["enabled"] = payload.enabled
            state["acknowledged_revision"] = revision
            state["acknowledged_event_id"] = event_id
            state["acknowledged_at"] = payload.occurred_at
            state["pending"] = None
            state["last_error"] = (
                "Gsensor reported a newer activation revision; the local pending "
                "request was superseded."
                if pending is not None
                else None
            )
            self._save_gsensor_activation_state(state, manifest=manifest, run_directory=run_directory)
            return True

        if pending is not None:
            if _timestamp_precedes(payload.occurred_at, pending["requested_at"]):
                return False
            if event_id != pending.get("event_id"):
                return False
            requested_enabled = pending["enabled"]
            state["enabled"] = payload.enabled
            state["acknowledged_revision"] = revision
            state["acknowledged_event_id"] = event_id
            state["acknowledged_at"] = payload.occurred_at
            state["pending"] = None
            if payload.enabled is requested_enabled:
                state["last_error"] = None
            else:
                state["last_error"] = (
                    "Gsensor acknowledged the command but remained disabled. "
                    "Refresh and issue a new enable request."
                    if requested_enabled
                    else "Gsensor did not confirm the requested disabled state."
                )
            self._save_gsensor_activation_state(state, manifest=manifest, run_directory=run_directory)
            return True

        acknowledged_revision = state["acknowledged_revision"]
        acknowledged_event_id = state.get("acknowledged_event_id")
        if revision != acknowledged_revision:
            return False
        acknowledged_at = state.get("acknowledged_at")
        if acknowledged_at is not None and _timestamp_precedes(
            payload.occurred_at,
            acknowledged_at,
        ):
            return False
        if acknowledged_event_id is not None and event_id != acknowledged_event_id:
            return False
        if payload.enabled is prior_enabled:
            state["acknowledged_event_id"] = event_id
            state["acknowledged_at"] = payload.occurred_at
            self._save_gsensor_activation_state(state, manifest=manifest, run_directory=run_directory)
            return True
        # Recovery is fail-safe: a restarted Gsensor restores disabled at the same
        # revision. That is authoritative and requires a fresh higher revision to
        # enable it again. The reverse transition is never accepted implicitly.
        if prior_enabled and not payload.enabled:
            state["enabled"] = False
            state["acknowledged_event_id"] = event_id
            state["acknowledged_at"] = payload.occurred_at
            state["last_error"] = (
                "Gsensor restarted in the disabled state. Issue a new enable request."
            )
            self._save_gsensor_activation_state(state, manifest=manifest, run_directory=run_directory)
            return True
        return False

    def run_configuration_payload(self, *, locked: bool | None = None) -> Dict[str, Any]:
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
            "locked": self._run_configuration_locked() if locked is None else locked,
            "source_file": str(self.run_configuration_store.path),
        }

    @_action_lock("_preparation_lock")
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
        # The same command verified editability under the preparation lock.
        # Reuse that result for its response; independent reads still revalidate.
        result = self.run_configuration_payload(locked=False)
        result["saved"] = True
        result["changed"] = changed
        return result

    def _run_configuration_locked(self) -> bool:
        manifest = self.experiments.current_manifest()
        if manifest is None:
            return False
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
        self.process_source.start()
        if not self._controller_poll_thread or not self._controller_poll_thread.is_alive():
            self._controller_poll_stop.clear()
            self._controller_poll_thread = threading.Thread(
                target=self._controller_monitor, name="central-controller-status", daemon=True)
            self._controller_poll_thread.start()
        self.archive.start()
        if not self._status_thread or not self._status_thread.is_alive():
            self._status_stop.clear()
            self._status_thread = threading.Thread(target=self._status_monitor, name="central-status", daemon=True)
            self._status_thread.start()
        if not self._runtime_monitor_thread or not self._runtime_monitor_thread.is_alive():
            self._runtime_monitor_stop.clear()
            self._runtime_monitor_thread = threading.Thread(target=self._runtime_monitor, name="central-runtime-events", daemon=True)
            self._runtime_monitor_thread.start()
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._consumer_thread = threading.Thread(
            target=self._consume_forever,
            name="central-rabbitmq-consumer",
            daemon=True,
        )
        self._consumer_thread.start()

    def stop(self) -> None:
        self._controller_poll_stop.set()
        if self._controller_poll_thread:
            self._controller_poll_thread.join(timeout=2)
        self._status_stop.set()
        if self._status_thread:
            self._status_thread.join(timeout=2)
        self.process_source.stop()
        self.archive.stop()
        self._runtime_monitor_stop.set()
        if self._runtime_monitor_thread:
            self._runtime_monitor_thread.join(timeout=3)
        self._ordinary_delivery.close()
        self._stop_delivery.close()

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

    @_action_lock("_message_lock", lifecycle=True, timeout=-1)
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
            is_completed_message = name == GROWTH_RATE_COMPLETED_MESSAGE
            is_completed_status = payload.status == GrowthRateStatus.COMPLETED
            if is_completed_message and not is_completed_status:
                raise ValueError("growth_rate.completed must use status='completed'.")
            if is_completed_status and not is_completed_message:
                raise ValueError("status='completed' must use growth_rate.completed.")
            if not self._apply_gsensor_status(payload):
                return {"accepted": True, "ignored": True, "reason": "Outdated Gsensor status."}
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

    def _apply_gsensor_status(self, payload: GrowthRateStatusPayload) -> bool:
        run_id = self.experiments.current_run_id()
        if run_id is None or payload.run_id != run_id:
            raise ValueError("Gsensor status run_id does not match the current experiment.")

        manifest = self.experiments.registry.get(run_id)
        if (
            payload.experiment_started_at is not None
            and manifest.started_at is not None
            and not _same_timestamp(payload.experiment_started_at, manifest.started_at)
        ):
            raise ValueError(
                "Gsensor status experiment_started_at does not match the current experiment."
            )
        if manifest.status in {ExperimentStatus.COMPLETED, ExperimentStatus.ERROR}:
            return False
        previous = self.last_gsensor_status
        if previous is not None and previous.get("run_id") == run_id:
            previous_revision = previous.get("control_revision")
            if type(previous_revision) is int:
                if payload.control_revision is None:
                    return False
                if payload.control_revision < previous_revision:
                    return False
            previous_frame = previous.get("frame_seq")
            if (
                payload.frame_seq is not None and previous_frame is not None
                and payload.frame_seq < previous_frame
            ):
                return False
            try:
                previous_time = datetime.fromisoformat(str(previous["occurred_at"]).replace("Z", "+00:00"))
                incoming_time = datetime.fromisoformat(payload.occurred_at.replace("Z", "+00:00"))
                if incoming_time < previous_time:
                    return False
            except (KeyError, TypeError, ValueError):
                # Older valid producers may not have a comparable timestamp.
                pass
        # Resolve once before holding activation state. Previously each load and
        # save scanned every experiment again while blocking status readers.
        run_directory = self.experiments.registry.run_dir(run_id)
        with self._activation_lock:
            if not self._observe_gsensor_activation(payload, manifest, run_directory=run_directory):
                return False
        if payload.enabled:
            self.archive.note_gsensor_enabled(run_id)
        status = payload.status
        if status == GrowthRateStatus.ERROR:
            self.experiments.registry.mark_error(
                run_id,
                error=payload.error or "Gsensor reported an error.",
            )
            with self._lock:
                self.operation_state["experiment_active"] = False
            return True

        if status == GrowthRateStatus.COMPLETED:
            manifest = self.experiments.registry.get(run_id)
            if manifest.status != ExperimentStatus.STOPPING:
                self.experiments.request_stop(run_id)
            self.experiments.finish(run_id)
            with self._lock:
                self.operation_state["experiment_active"] = False
            return True

        if status == GrowthRateStatus.DISABLED:
            # Gsensor activation is independent from the experiment lifecycle.
            # Disabling image processing must not stop or complete Controller work.
            return True

        if (
            manifest.status == ExperimentStatus.MEASURING
            and status in {
                GrowthRateStatus.INITIALIZING,
                GrowthRateStatus.BASELINE_READY,
            }
        ):
            # Runtime re-marking starts a new Gsensor processing segment while
            # the overall experiment and Controller remain in their running phase.
            return True

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
            return True
        if not self._advance_experiment_status(run_id, target):
            return False
        with self._lock:
            self.operation_state["experiment_active"] = target not in {
                ExperimentStatus.STOPPING,
            }
        return True

    def _advance_experiment_status(
        self,
        run_id: str,
        target: ExperimentStatus,
    ) -> bool:
        ordered = [
            ExperimentStatus.STARTING,
            ExperimentStatus.WAITING_FOR_INITIAL_IMAGE,
            ExperimentStatus.INITIALIZING,
            ExperimentStatus.MEASURING,
            ExperimentStatus.STOPPING,
        ]
        manifest = self.experiments.registry.get(run_id)
        if manifest.status == target:
            return True
        if manifest.status in {ExperimentStatus.COMPLETED, ExperimentStatus.ERROR}:
            raise InvalidExperimentStateError(
                f"Cannot apply Gsensor status to {manifest.status.value} experiment."
            )
        current_index = ordered.index(manifest.status)
        target_index = ordered.index(target)
        if target_index < current_index:
            return False
        for next_status in ordered[current_index + 1 : target_index + 1]:
            self.experiments.transition(run_id, next_status)
        return True

    def controller_status(self) -> Dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._controller_observation)
            observed = self._controller_observed_monotonic
        result["age_s"] = None if observed is None else max(0, time.monotonic() - observed)
        result["stale"] = observed is None or result["age_s"] > self.controller_observation_max_age_sec
        if result["stale"]:
            result.update(available=False, error="Controller status is stale; waiting for background observation.")
        return result

    def _fetch_controller_status(self) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(self.controller_status_url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Controller status response must be an object.")
            return {**payload, "available": True, "error": None}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "available": False,
                "status": "unavailable",
                "active": False,
                "error": str(exc),
            }

    def gsensor_status(self) -> Dict[str, Any]:
        """Read this participant now; cached AMQP errors never authorize End."""
        url = getattr(self, "gsensor_status_url", None)
        if not url:
            return {"available": False, "error": "Gsensor status URL is not configured."}
        try:
            request = urllib.request.Request(url, headers={"Cache-Control": "no-cache, no-store"})
            with urllib.request.urlopen(request, timeout=1.0) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Gsensor status response exceeds the bounded read size.")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Gsensor status response must be an object.")
            return {**payload, "available": True, "error": None}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {"available": False, "error": str(exc)}

    def _collect_system_status(self, controller) -> Dict[str, Any]:
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
            same_run = bool(self.last_gsensor_status and self.last_gsensor_status.get("run_id") == current_run_id)
            gsensor = {
                "last_status": copy.deepcopy(self.last_gsensor_status) if same_run else None,
                "received_at": self.last_gsensor_status_received_at if same_run else None,
                "consumer_error": self.last_status_consumer_error,
                "last_rejection_error": self.last_rejected_status_error,
                "message_count": self.status_message_count,
                "rejection_count": self.status_rejection_count,
            }
        gsensor["activation"] = self.gsensor_activation_status(experiment=current)
        # Full history has its own monitor. This background snapshot refresh
        # reconciles the latest Controller observation and durable request.
        request = self.runtime_request_store.observe(current_run_id, controller)
        errors = [self.runtime_history_error, self.runtime_request_store.observation_error]
        try:
            history = self.runtime_request_store.history.latest_fields(current_run_id) if current_run_id else []
        except (OSError, ValueError, KeyError, TypeError) as exc:
            history = []
            detail = str(exc) if isinstance(exc, RuntimeHistoryEventError) else type(exc).__name__
            errors.append(f"History unavailable: {detail}")
        archive = self.archive.status_summary(current) if current else None
        return {
            "observed_at": utc_ts(),
            "current_experiment": current,
            "experiments": experiments,
            "gsensor": gsensor,
            "controller": controller,
            "process_source": self.process_source.status(),
            "runtime_request": request,
            "runtime_history": history,
            "runtime_history_error": "; ".join(dict.fromkeys(filter(None, errors))) or None,
            "archive": archive["archive"] if archive else None,
            "archive_error": (archive.get("archive_error") if archive else None) or self.archive.error,
        }
    def overlay_path(self, run_id: str, kind: str) -> Path:
        if kind not in {"latest", "final"}:
            raise ValueError("Overlay kind must be 'latest' or 'final'.")
        manifest = self.experiments.registry.get(run_id)
        run_directory = self.experiments.registry.run_dir(manifest.run_id)
        filename = LATEST_OVERLAY_FILENAME if kind == "latest" else FINAL_OVERLAY_FILENAME
        processing_state_path = run_directory / GSENSOR_PROCESSING_STATE_FILENAME
        if processing_state_path.is_file():
            try:
                processing_state = json.loads(
                    processing_state_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Could not read Gsensor processing state: {processing_state_path}"
                ) from exc
            if (
                not isinstance(processing_state, dict)
                or processing_state.get("run_id") != manifest.run_id
            ):
                raise ValueError(
                    "Gsensor processing-state run_id does not match the experiment."
                )
            configured = processing_state.get(f"{kind}_overlay_path")
            if configured is None:
                raise ExperimentNotFoundError(
                    f"{kind.capitalize()} overlay is not available yet."
                )
            if not isinstance(configured, str) or not configured.strip():
                raise ValueError("Gsensor processing-state overlay path is invalid.")
            configured_path = Path(configured)
            path = (
                configured_path
                if configured_path.is_absolute()
                else run_directory / configured_path
            ).resolve()
        else:
            # Legacy runs wrote the stable overlays directly in their run root.
            path = (run_directory / filename).resolve()
        try:
            path.relative_to(run_directory)
        except ValueError as exc:
            raise InvalidExperimentIdentifierError("Overlay path escapes experiment.") from exc
        if not path.is_file():
            raise ExperimentNotFoundError(f"{kind.capitalize()} overlay is not available yet.")
        return path

    def load_params(self) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
        try:
            self._require_parameter_draft_editable()
        except InvalidExperimentStateError:
            # STARTING retries must keep the already prepared snapshot/version.
            return load_params(str(self._active_params_path()))
        return load_runtime_params(str(self.params_path), str(self.default_params_path))

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

    @_action_lock("_preparation_lock")
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

    def parameter_document_revision(self):
        return hashlib.sha256(repr(tuple((str(path), file_version(path)) for path in
            (self.default_params_path, self.params_path, self.param_meta_path))).encode()).hexdigest()

    def params_payload(self) -> Dict[str, Any]:
        revision = self.parameter_document_revision()
        shared, gsensor, controller, version = self.load_params()
        default_shared, default_gsensor, default_controller, default_version = (
            self.load_default_params()
        )
        manifest = self.experiments.current_manifest()
        result = {
            "document_revision": revision,
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
            "policies": parameter_policies(),
            "derived_policies": derived_parameter_policies(),
            "status": self.parameter_status(
                shared=shared,
                gsensor=gsensor,
                controller=controller,
                version=version,
                defaults=(default_shared, default_gsensor, default_controller),
                manifest=manifest,
            ),
        }
        if manifest is not None and manifest.status not in {
            ExperimentStatus.CREATED, ExperimentStatus.COMPLETED, ExperimentStatus.ERROR,
        }:
            controller_status = self.controller_status()
            live = controller_status.get("run_parameters") or {}
            identity_matches = (controller_status.get("available")
                                and controller_status.get("current_run_id") == manifest.run_id
                                and _same_recovery_timestamp(controller_status.get("started_at"), manifest.started_at))
            matching = (identity_matches and live.get("supported")
                        and all(isinstance((live.get("values") or {}).get(section), dict)
                                for section in ("shared", "gsensor", "controller")))
            result["run_parameters"] = {
                **live, "supported": bool(matching), "run_id": manifest.run_id,
                "experiment_started_at": manifest.started_at,
            }
            if identity_matches:
                result["derived_values"] = live.get("derived_values")
            if matching:
                for section in ("shared", "gsensor", "controller"):
                    result[section] = dict((live.get("values") or {}).get(section) or result[section])
                result["policies"] = live.get("policies") or result["policies"]
                result["status"] = {"kind": "run_parameters", "message":
                    f"Current experiment parameters · revision {live.get('revision', 0)}"}
            else:
                result["status"] = {"kind": "unavailable", "message": live.get("unavailable_reason")
                                    or "Current run parameters are not confirmed; editing is locked."}
        return result

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
        manifest: ExperimentManifest | None = None,
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
        if manifest is None:
            manifest = self.experiments.current_manifest()
        if (
            manifest is not None
            and manifest.params_snapshot_file
            and manifest.parameter_version == version
        ):
            applied_run_id = manifest.run_id
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

    @_action_lock("_preparation_lock")
    def save_params(
        self,
        payload: ParamsUpdate,
        *,
        allow_changes: bool = True,
        allow_locked: bool = False,
    ) -> Dict[str, Any]:
        if payload.changes is not None:
            return self.update_run_parameters(payload)
        if not allow_locked:
            self._require_parameter_draft_editable()
        current_shared, current_gsensor, current_controller, current_version = (
            self.load_params()
        )
        if int(payload.version) != current_version:
            raise ParameterValidationError(
                "The parameter draft changed on the server. Reload it before saving."
            )
        if not allow_changes:
            # A STARTING retry resends the saved draft, which may predate new
            # default fields. It must neither migrate nor validate against a
            # newer schema; the immutable snapshot will be checked on start.
            if (payload.shared, payload.gsensor, payload.controller) != (
                current_shared, current_gsensor, current_controller,
            ):
                raise InvalidExperimentStateError(
                    "This experiment has already started; its parameter snapshot is immutable."
                )
            result = self.params_payload()
            result["saved"] = True
            result["changed"] = False
            return result
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

    @_action_lock("_runtime_action_lock")
    def update_run_parameters(self, payload: ParamsUpdate) -> Dict[str, Any]:
        """Persist a run-local delta before delivery; never write the startup draft."""
        run_id, controller = self._require_running_controller("Save Parameters")
        manifest = self.experiments.current_manifest()
        if (payload.expected_run_id != run_id or not payload.event_id
                or payload.expected_revision is None or manifest is None
                or payload.experiment_started_at != manifest.started_at):
            raise InvalidExperimentStateError("Parameter update experiment identity changed; refresh before saving.")
        live = controller.get("run_parameters") or {}
        runtime = controller.get("runtime_controls") or {}
        if (not live.get("supported")
                or not _same_recovery_timestamp(controller.get("started_at"), manifest.started_at)):
            raise InvalidExperimentStateError(live.get("unavailable_reason")
                                              or "Controller does not support online parameter updates.")
        # Preserve the exact original request on retry, including requested_at.
        prior = self.runtime_request_store.observe(run_id, controller)
        original = (self.runtime_request_store.get_event(run_id, payload.event_id) or {}).get("command") or {}
        event = RunParametersUpdatePayload.from_mapping({
            "run_id": run_id, "event_id": payload.event_id,
            "expected_revision": payload.expected_revision,
            "changes": payload.changes,
            "requested_at": payload.requested_at or original.get("requested_at") or utc_ts(),
            "experiment_started_at": payload.experiment_started_at,
        })
        known = self._known_runtime_request(event, controller, prior)
        if known:
            if known["status"] in {"applied", "rejected"}:
                return {**self.params_payload(), "requested": False, "runtime_request": known}
            entry = known
        else:
            if prior and prior["status"] not in {"applied", "rejected"}:
                self._record_runtime_rejection(event, runtime, "A runtime request is unconfirmed; retry that same event.")
            if runtime.get("pending") or live.get("pending"):
                self._record_runtime_rejection(event, runtime, "A runtime update is pending; wait for confirmation.")
            if event.expected_revision != runtime.get("revision"):
                self._record_runtime_rejection(event, runtime, "Runtime revision changed; refresh before editing.")
            try:
                validate_parameter_changes(event.changes, current=live.get("values"))
            except ValueError as exc:
                raise ParameterValidationError(str(exc)) from exc
            entry = {"command": event.to_dict(), "status": "pending", "created_at": time.time(),
                     "result": None, "transport_error": None}
        self.runtime_request_store.save(run_id, entry)
        entry = self._deliver_runtime(event, "publish_run_parameters_command")
        return {**self.params_payload(), "requested": True, "runtime_request": entry}

    def _require_parameter_draft_editable(self) -> None:
        manifest = self.experiments.current_manifest()
        if manifest is None:
            return
        if manifest.status not in {
            ExperimentStatus.CREATED,
            ExperimentStatus.COMPLETED,
            ExperimentStatus.ERROR,
        }:
            raise InvalidExperimentStateError(
                "Parameters are locked after an experiment starts. Create a new experiment "
                "before preparing another parameter draft."
            )

    @_action_lock("_preparation_lock")
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

    def operation_payload(self, revision=None):
        def current_revision():
            manifest = self.experiments.current_manifest()
            identity = (manifest.run_id, manifest.started_at, manifest.status.value) if manifest else None
            return hashlib.sha256(json.dumps([self.parameter_document_revision(), identity,
                self.run_configuration.to_dict(), self.run_configuration_store.updated_at,
                self.operation_state], sort_keys=True).encode()).hexdigest()
        with self._operation_cache_lock:
            version = current_revision()
            if revision == version:
                return {"revision": version, "unchanged": True}
            if self._operation_cache and self._operation_cache[0] == version:
                return copy.deepcopy(self._operation_cache[1])
            payload = {"target": self.target, "state": dict(self.operation_state),
                       "run_configuration": self.run_configuration_payload(),
                       "parameters_revision": self.parameter_document_revision(),
                       "preview": self.preview_publish_payload(), "revision": None}
            if version == current_revision():
                payload["revision"] = version
                self._operation_cache = (version, copy.deepcopy(payload))
            return payload

    def runtime_request_status(self, run_id, event_id):
        self.experiments.registry.get(run_id)
        entry = self.runtime_request_store.get_event(run_id, event_id)
        return self.runtime_request_store.project(entry, self.controller_status()) if entry else None

    @_action_lock("_preparation_lock", lifecycle=True)
    def create_experiment(self, *, label: str | None = None) -> Dict[str, Any]:
        """Prepare a new run without starting either experiment service."""

        self._require_experiment_switch_allowed()
        experiment = self.experiments.create(label=label)
        try:
            delivery, command = self._deliver(f"select:{experiment['run_id']}", experiment["run_id"],
                lambda p: p.publish_experiment_select_command(experiment["run_id"],
                                                              image_directory=experiment["image_directory"]))
            self._require_delivery(delivery)
        except (DeliveryBusy, DeliveryCancelled):
            raise
        except Exception as exc:
            raise RuntimeError(
                "The experiment was created, but its selection could not be delivered "
                "to Gsensor. Keep this experiment and use Retry Selection before Start."
            ) from exc
        return {**experiment, "selection_command": command or {}, "delivery": delivery}

    @_action_lock("_preparation_lock", lifecycle=True)
    def select_experiment(self, run_id: str) -> Dict[str, Any]:
        self._require_experiment_switch_allowed(run_id)
        experiment = self.experiments.select(run_id)
        try:
            delivery, command = self._deliver(f"select:{run_id}", run_id,
                lambda p: p.publish_experiment_select_command(run_id, image_directory=experiment["image_directory"]))
            self._require_delivery(delivery)
        except (DeliveryBusy, DeliveryCancelled):
            raise
        except Exception as exc:
            raise RuntimeError(
                "The experiment is selected in Central, but delivery to Gsensor failed. "
                "Keep this experiment and use Retry Selection before Start."
            ) from exc
        return {**experiment, "selection_command": command or {}, "delivery": delivery}

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

    def _require_controller_start_ready(self, run_id: str) -> dict[str, Any]:
        """Reject known startup blockers before locking or publishing a run."""

        controller = self.controller_status()
        if not controller.get("available"):
            raise RuntimeError(
                "Controller is unavailable; the experiment was not started. "
                "Restore the Controller service and retry Start."
            )
        recovery = controller.get("recovery") or {}
        if recovery.get("status") == "error":
            reason = recovery.get("error") or "The saved Controller state is incompatible."
            raise InvalidExperimentStateError(
                f"Controller checkpoint recovery failed: {reason} "
                "The experiment was not started. Preserve the old session directory, "
                "set EXPERIMENT_HOST_ROOT to a new directory, and recreate Central, "
                "Controller and GSensor before starting a new experiment."
            )
        if (controller.get("status") != "running"
                and controller.get("current_run_id") and controller.get("stopped_at") is None
                and _same_recovery_timestamp(controller.get("started_at"), controller.get("started_at"))):
            raise InvalidExperimentStateError(
                f"Controller has an unended failed experiment {controller['current_run_id']}. "
                "Select that experiment and use End Experiment, then wait for Controller "
                "to confirm stopped before starting a new experiment. The selected experiment was not started."
            )
        if (controller.get("adapter") or {}).get("safe_noop"):
            raise InvalidExperimentStateError(
                "The Controller algorithm adapter is not enabled. Set "
                "CONTROLLER_ADAPTER to the algorithm implementation and recreate "
                "Controller before starting an experiment."
            )
        if (controller.get("status") == "running"
                and controller.get("current_run_id") != run_id):
            raise InvalidExperimentStateError(
                "Controller is running another experiment. End that experiment "
                "before starting the selected Central experiment."
            )
        return controller

    @_action_lock("_preparation_lock", lifecycle=True)
    def start_experiment(self, params: ParamsUpdate | None = None) -> Dict[str, Any]:
        run_id = self.experiments.current_run_id()
        if run_id is None:
            raise InvalidExperimentStateError(
                "Create or select an experiment before starting it."
            )
        manifest = self.experiments.registry.get(run_id)
        self._require_run_open(run_id)
        if manifest.status not in {ExperimentStatus.CREATED, ExperimentStatus.STARTING}:
            raise InvalidExperimentStateError(
                f"Cannot start experiment {run_id} from state {manifest.status.value}."
            )
        controller = self._require_controller_start_ready(run_id)
        if params is not None:
            self.save_params(
                params,
                allow_changes=manifest.status == ExperimentStatus.CREATED,
                allow_locked=manifest.status == ExperimentStatus.STARTING,
            )
        raw_shared, raw_gsensor, raw_controller, raw_version = self.load_params()
        raw_sections = {"shared": raw_shared, "gsensor": raw_gsensor, "controller": raw_controller}
        params_snapshot = self.preview_publish_payload()
        if raw_version != int(params_snapshot["version"]):
            raise InvalidExperimentStateError("Parameter draft changed while preparing startup; reload before starting.")
        if (
            manifest.status == ExperimentStatus.STARTING
            and manifest.parameter_version != int(params_snapshot["version"])
        ):
            raise InvalidExperimentStateError(
                "The prepared experiment parameter version no longer matches the "
                "saved parameter draft. End this experiment and create a new one."
            )
        if manifest.status == ExperimentStatus.STARTING:
            directory = self.experiments.registry.run_dir(run_id).resolve()
            path = (directory / (manifest.params_snapshot_file or "params_snapshot.yaml")).resolve()
            if path.parent != directory or not path.is_file():
                raise InvalidExperimentStateError("The prepared parameter snapshot is unavailable; startup cannot be retried.")
            prepared = load_params_document(str(path))
            recorded_raw = prepared.get("raw_parameter_sections")
            if recorded_raw is not None:
                params_snapshot["raw_parameter_sections"] = raw_sections
            if prepared != params_snapshot:
                raise InvalidExperimentStateError(
                    "The parameter draft differs from the immutable prepared snapshot, including standby values. "
                    "Restore the prepared draft or end this experiment before creating another."
                )
            # Retry only the exact prepared values. Old snapshots have no full
            # raw catalog: keep their old flat protocol, do not invent metadata.
            params_snapshot = prepared
            raw_sections = recorded_raw
        else:
            params_snapshot["raw_parameter_sections"] = raw_sections
        self._require_run_open(run_id)
        experiment = self.experiments.start(
            run_id,
            params_snapshot=params_snapshot,
            parameter_version=int(params_snapshot["version"]),
        )
        started_manifest = self.experiments.registry.get(run_id)
        self.archive.capture_start(run_id, self.run_configuration.to_dict(), controller)
        self._load_gsensor_activation_state(started_manifest, create=True)
        # STARTING is intentionally persisted before RabbitMQ delivery. If a
        # publish is interrupted, the immutable snapshot can be retried while
        # configuration changes and experiment switching remain locked.
        self.operation_state["experiment_active"] = True
        configuration = self.run_configuration
        controller_runtime = {
                **params_snapshot["controller"],
                "mode": configuration.controller_mode,
                "exp_sim": configuration.run_type,
                "target": configuration.control_target,
                "adaptive_mode": configuration.adaptation_mode,
                "exp_sim_G": {
                    "live_gsensor": "experiment",
                    "simulated": "simulation",
                    "presaved_images": "experiment_with_presaved",
                }[configuration.growth_rate_source],
            }
        def deliver(publisher):
            parameter_messages = publisher.publish_params(
                params_snapshot["shared"],
                params_snapshot["gsensor"],
                controller_runtime,
                int(params_snapshot["version"]),
                parameter_sections=raw_sections,
                include_parameter_sections=raw_sections is not None,
                experiment=experiment,
            )
            self._require_run_open(run_id)
            commands = publisher.publish_experiment_start_command(
                experiment,
                adaptation_enabled=configuration.adaptation_enabled,
                adaptation_mode=configuration.adaptation_mode,
            )
            return {"parameter_messages": parameter_messages, "commands": commands}
        try:
            delivery, messages = self._deliver(f"start:{run_id}", run_id, deliver)
            self._require_delivery(delivery)
        except (DeliveryBusy, DeliveryCancelled):
            raise
        except Exception as exc:
            raise RuntimeError(
                "The experiment snapshot was saved, but service startup delivery did "
                "not complete. Keep this experiment selected and retry Start."
            ) from exc
        return {
            "triggered": True,
            "startup_status": delivery["status"],
            "delivery": delivery,
            "key": "experiment_active",
            "value": True,
            "parameter_messages": (messages or {}).get("parameter_messages", {}),
            "parameters": self.params_payload(),
            "commands": (messages or {}).get("commands", {}),
            "experiment": experiment,
            "gsensor_activation": self.gsensor_activation_status(),
        }

    @_action_lock("_end_lock", lifecycle=True)
    def finish_experiment(self, run_id: str) -> Dict[str, Any]:
        manifest = self.experiments.registry.get(run_id)
        current_run_id = self.experiments.current_run_id()
        if manifest.status not in {ExperimentStatus.COMPLETED, ExperimentStatus.ERROR} and run_id != current_run_id:
            raise InvalidExperimentStateError("Only the current experiment can be ended while it is active.")
        with self._lock:
            self._stopping_runs.add(run_id)
        self._ordinary_delivery.cancel_run(run_id)
        deliveries = []

        def publish_stop(**kwargs):
            target = "controller" if kwargs.get("controller_only") else "gsensor" if kwargs.get("gsensor_only") else "both"
            delivery, _ = self._deliver(f"stop:{run_id}:{target}", run_id,
                lambda p: p.publish_experiment_stop_command(run_id, **kwargs), stop=True)
            deliveries.append(delivery)
            self._require_delivery(delivery)

        if manifest.status in {ExperimentStatus.COMPLETED, ExperimentStatus.ERROR}:
            experiment = self.experiments.get(run_id)
            # An explicit End may still need to finish this exact interrupted
            # Controller session. Do not infer ownership from history alone.
            controller = self._fetch_controller_status()
            controller_started_at = controller.get("started_at")
            recovery = controller.get("recovery") or {}
            interrupted = (
                controller.get("status") in {"error", "configured"}
                and controller.get("stopped_at") is None
                and isinstance(recovery, dict) and recovery.get("status") != "error"
            )
            matching_interrupted = (
                controller.get("available") is True
                and interrupted
                and controller.get("active") is False
                and controller.get("current_run_id") == run_id
                and isinstance(manifest.started_at, str) and bool(manifest.started_at)
                and isinstance(controller_started_at, str) and bool(controller_started_at)
                and _same_recovery_timestamp(controller_started_at, manifest.started_at)
            )
            if matching_interrupted:
                try:
                    publish_stop(
                        experiment_started_at=manifest.started_at,
                        reason="central_end_failed_controller", controller_only=True,
                    )
                except (DeliveryBusy, DeliveryCancelled):
                    raise
                except Exception as exc:
                    raise RuntimeError(
                        "Recovery stop delivery did not complete. The experiment remains terminal; "
                        "retry End for this same run after checking Controller status."
                    ) from exc
                experiment["recovery_stop"] = {
                    "requested": True, "status": deliveries[-1]["status"], "run_id": run_id,
                    "experiment_started_at": manifest.started_at,
                }
            # An error notification can terminate the registry before Gsensor
            # has received End and persisted its producer-finalization marker.
            # Judge it independently: Controller may already be stopped. Never
            # broadcast a stop for a historical ERROR or rely on an old AMQP
            # status to infer that Gsensor still owns this experiment.
            if manifest.status == ExperimentStatus.ERROR and run_id == current_run_id:
                gsensor = self.gsensor_status()
                gsensor_started_at = gsensor.get("experiment_started_at")
                activation = gsensor.get("gsensor_activation")
                matching_gsensor = (
                    gsensor.get("available") is True
                    and gsensor.get("role") == "gsensor"
                    and gsensor.get("active") is False
                    and gsensor.get("measurement_running") is False
                    and isinstance(activation, dict) and activation.get("enabled") is False
                    # Production Gsensor exposes the activation gate but has
                    # no top-level readiness field. When present (Synthetic G),
                    # readiness must explicitly be false, never true or null.
                    and ("measurement_ready" not in gsensor or gsensor["measurement_ready"] is False)
                    and gsensor.get("experiment_lifecycle_status") == "error"
                    and gsensor.get("current_run_id") == run_id
                    and isinstance(manifest.started_at, str) and bool(manifest.started_at)
                    and isinstance(gsensor_started_at, str) and bool(gsensor_started_at)
                    and _same_recovery_timestamp(gsensor_started_at, manifest.started_at)
                    and self.experiments.current_run_id() == run_id
                )
                if matching_gsensor:
                    try:
                        publish_stop(
                            experiment_started_at=manifest.started_at,
                            reason="central_end_gsensor_finalization", gsensor_only=True,
                        )
                    except (DeliveryBusy, DeliveryCancelled):
                        raise
                    except Exception as exc:
                        raise RuntimeError(
                            "Gsensor finalization stop delivery did not complete. The experiment remains terminal; "
                            "retry End for this same run after checking Gsensor status."
                        ) from exc
                    experiment["gsensor_finalization_stop"] = {
                        "requested": True, "status": deliveries[-1]["status"], "run_id": run_id,
                        "experiment_started_at": manifest.started_at,
                    }
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
                publish_stop(
                    experiment_started_at=manifest.started_at,
                    reason=(
                        "central_retry_end_experiment"
                        if retrying
                        else "central_end_experiment"
                    ),
                )
            except (DeliveryBusy, DeliveryCancelled):
                raise
            except Exception as exc:
                raise RuntimeError(
                    "The experiment is marked as stopping, but service stop delivery "
                    "did not complete. Keep this experiment selected and retry End."
                ) from exc

        if run_id == current_run_id:
            self.operation_state["experiment_active"] = False
        if deliveries:
            experiment["delivery"] = deliveries[-1]
        return experiment

    @_action_lock("_runtime_action_lock")
    def add_seed(self) -> Dict[str, Any]:
        """Record one operator-confirmed seed addition in the live Controller."""

        run_id, _controller = self._require_running_controller("Add Seed")
        event_id, added_at = uuid4().hex, utc_ts()
        command = CentralApp.build_controller_add_seed_command(self.publisher, run_id, event_id=event_id, added_at=added_at)
        delivery, sent = self._deliver(f"seed:{run_id}:{event_id}", run_id,
            lambda p: p.publish_controller_add_seed_command(run_id, event_id=event_id, added_at=added_at))
        self._require_delivery(delivery)
        return {
            "requested": True,
            "delivery": delivery,
            "event": dict(command["payload"]),
            "command": sent or command,
        }

    @_action_lock("_runtime_action_lock")
    def update_controller_runtime(self, event: ControllerRuntimeUpdatePayload) -> Dict[str, Any]:
        # Only a previously unknown event can be declared definitively unsent.
        # A retry may have reached Controller before its HTTP response was lost.
        previous = self.runtime_request_store.get_event(event.run_id, event.event_id)
        try:
            run_id, entry = self._prepare_runtime_update(event)
        except (ControllerUnavailableError, InvalidExperimentStateError) as exc:
            if previous is None:
                raise RuntimeRequestNotSent(event, exc) from exc
            raise
        if entry["status"] in {"applied", "rejected"}:
            return {"requested": False, "runtime_request": entry}
        # Persist the exact event before delivery; an interrupted request can be
        # recovered after refresh or Central restart without a new event ID.
        self.runtime_request_store.save(run_id, entry)
        entry = self._deliver_runtime(event, "publish_controller_runtime_command")
        return {"requested": True, "runtime_request": entry}

    def _prepare_runtime_update(self, event):
        run_id, controller = self._require_running_controller("Runtime controls")
        if event.run_id != run_id:
            raise InvalidExperimentStateError("Runtime run_id does not match the current experiment.")
        runtime = controller.get("runtime_controls") or {}
        if not runtime.get("supported"):
            raise InvalidExperimentStateError("Controller does not support runtime updates.")
        prior = self.runtime_request_store.observe(run_id, controller)
        known = self._known_runtime_request(event, controller, prior)
        if known:
            entry = known
        else:
            if prior and prior["status"] not in {"applied", "rejected"}:
                self._record_runtime_rejection(event, runtime, "A runtime request is unconfirmed; retry that same event.")
            if runtime.get("pending") or (controller.get("run_parameters") or {}).get("pending"):
                self._record_runtime_rejection(event, runtime, "A runtime update is pending; wait for confirmation.")
            if event.expected_revision != runtime.get("revision"):
                self._record_runtime_rejection(event, runtime, "Runtime revision changed; refresh before editing.")
            entry = {
                "command": event.to_dict(), "status": "pending",
                "created_at": time.time(), "result": None, "transport_error": None,
            }
        return run_id, entry

    def _known_runtime_request(self, event, controller, latest):
        known = self.runtime_request_store.get_event(event.run_id, event.event_id)
        if known and known["command"] != event.to_dict():
            raise InvalidExperimentStateError("Runtime event_id was reused with different content.")
        known = self.runtime_request_store.project(known, controller) if known else None
        if (known and known["status"] not in {"applied", "rejected"}
                and latest and latest["command"]["event_id"] != event.event_id):
            raise InvalidExperimentStateError("A newer runtime request is recorded; inspect this event's confirmation before retrying.")
        return known

    def _record_runtime_rejection(self, event, runtime, reason):
        # Preserve the existing in-flight request; record this separate refusal
        # only in history. Central rejection is not a Controller application.
        self.runtime_request_store.history.save({
            "command": event.to_dict(), "status": "rejected", "created_at": time.time(),
            "result": {"run_id": event.run_id, "event_id": event.event_id, "status": "rejected",
                       "source": "central", "reason": reason, "processed_at": utc_ts(),
                       "revision": runtime.get("revision"), "before": runtime.get("configuration"),
                       "configuration": runtime.get("configuration")}})
        raise InvalidExperimentStateError(reason)

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
        if controller.get("stale") or not controller.get("available"):
            controller = self._refresh_controller_observation()
        if not controller.get("available"):
            raise ControllerUnavailableError(
                f"Controller is unavailable; {action_label} was not sent."
            )
        # Identity/lifecycle may have changed during the bounded live query.
        if self.experiments.current_run_id() != run_id:
            raise InvalidExperimentStateError("The selected experiment changed; refresh before retrying.")
        latest_manifest = self.experiments.registry.get(run_id)
        if latest_manifest.status not in active_statuses or run_id in self._stopping_runs:
            raise InvalidExperimentStateError("Experiment ended before the command could be sent.")
        if controller.get("status") != "running":
            raise InvalidExperimentStateError(
                f"Controller is not ready for {action_label}."
            )
        if controller.get("current_run_id") != run_id:
            raise InvalidExperimentStateError(
                "Controller run_id does not match the current Central experiment."
            )
        if (controller.get("started_at") and latest_manifest.started_at
                and not _same_timestamp(controller["started_at"], latest_manifest.started_at)):
            raise InvalidExperimentStateError("Controller start identity does not match the current experiment.")
        return run_id, controller


def _retain_last_gsensor_frame(
    previous: Dict[str, Any] | None,
    current: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep the latest frame reference when a lifecycle status omits it."""

    merged = dict(current)
    if previous is None or previous.get("run_id") != merged.get("run_id"):
        return merged
    if merged.get("frame_seq") is None:
        merged["frame_seq"] = previous.get("frame_seq")
    if not merged.get("image_name"):
        merged["image_name"] = previous.get("image_name")
    return merged


def _same_recovery_timestamp(left: str, right: str) -> bool:
    """Hold-release identity must use two valid, timezone-aware instants."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        left_time = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_time = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return left_time.tzinfo is not None and right_time.tzinfo is not None and left_time == right_time


def _same_timestamp(left: str, right: str) -> bool:
    """Compare ISO timestamps while accepting equivalent UTC spellings."""

    try:
        left_time = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_time = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return left == right
    return left_time == right_time


def _timestamp_precedes(left: str, right: str) -> bool:
    """Return whether one ISO timestamp is older, treating naive values as UTC."""

    try:
        left_time = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_time = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "Gsensor activation status timestamps must use ISO-8601 text."
        ) from exc
    if left_time.tzinfo is None:
        left_time = left_time.replace(tzinfo=timezone.utc)
    if right_time.tzinfo is None:
        right_time = right_time.replace(tzinfo=timezone.utc)
    return left_time < right_time


def _atomic_write_json(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
def get_ui_config() -> Dict[str, Any]:
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
        with service.ui_action(payload):
            return service.update_run_configuration(payload)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/params")
def update_params(payload: ParamsUpdate) -> Dict[str, Any]:
    try:
        with service.ui_action(payload):
            return service.save_params(payload)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/params/reset")
def reset_params(payload: ExperimentContext | None = None) -> Dict[str, Any]:
    try:
        with service.ui_action(payload):
            return service.reset_params_to_defaults()
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/experiments", status_code=201)
def create_experiment(payload: ExperimentCreateRequest) -> Dict[str, Any]:
    try:
        with service.ui_action(payload):
            return service.create_experiment(label=payload.label)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/experiments")
def list_experiments() -> Dict[str, Any]:
    try:
        return service.experiments.list()
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/experiment-history")
def experiment_history(
    q: str = Query(default="", max_length=200),
    date_from: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> Dict[str, Any]:
    try:
        return service.archive.history(q, date_from, date_to)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/experiments/{run_id}/archive")
def experiment_archive(run_id: str) -> Dict[str, Any]:
    try:
        return service.archive.describe(run_id)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/experiments/{run_id}/export", status_code=202)
def export_experiment(run_id: str) -> Dict[str, Any]:
    try:
        return service.archive.request(run_id)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/experiments/{run_id}/exports/{filename}")
def download_experiment_csv(run_id: str, filename: str) -> FileResponse:
    try:
        path = service.archive.download_path(run_id, filename)
        return FileResponse(path, media_type="text/csv; charset=utf-8", filename=path.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
def select_experiment(run_id: str, payload: ExperimentContext | None = None) -> Dict[str, Any]:
    try:
        with service.ui_action(payload):
            return service.select_experiment(run_id)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/experiments/{run_id}/finish")
def finish_experiment(run_id: str, payload: ExperimentContext | None = None) -> Dict[str, Any]:
    try:
        with service.ui_action(payload):
            return service.finish_experiment(run_id)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/operation/state")
def get_operation_state(revision: str | None = None) -> Dict[str, Any]:
    return service.operation_payload(revision)


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
        with service.ui_action(payload):
            result = service.update_operation_value("target", payload.target)["run_configuration"]
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
        with service.ui_action(payload):
            return service.update_operation_value(payload.key, payload.value)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/operation/experiment/start")
def start_experiment(payload: Optional[ParamsUpdate] = None) -> Dict[str, Any]:
    try:
        with service.ui_action(payload):
            return service.start_experiment(payload)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/operation/gsensor/activation")
def set_gsensor_activation(payload: GsensorActivationRequest) -> Dict[str, Any]:
    try:
        return service.set_gsensor_activation(payload)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.post("/api/operation/controller/add-seed")
def add_seed(payload: ExperimentContext | None = None) -> Dict[str, Any]:
    try:
        with service.ui_action(payload):
            return service.add_seed()
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc

@web_app.post("/api/operation/controller/runtime")
def update_controller_runtime(payload: ControllerRuntimeRequest) -> Dict[str, Any]:
    try:
        event = ControllerRuntimeUpdatePayload.from_mapping(payload.model_dump())
        with service.ui_action(ExperimentContext(expected_run_id=event.run_id)):
            return service.update_controller_runtime(event)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/operation/controller/runtime/history")
def runtime_history(run_id: str, before: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=100)):
    try:
        return service.runtime_history_page(run_id, before, limit)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc


@web_app.get("/api/operation/controller/runtime/request")
def runtime_request_status(run_id: str, event_id: str) -> Dict[str, Any]:
    try:
        entry = service.runtime_request_status(run_id, event_id)
    except Exception as exc:
        raise _experiment_http_exception(exc) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail="Runtime request not found for this experiment.")
    return {"runtime_request": entry}


def _experiment_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, RuntimeRequestNotSent):
        status = 503 if isinstance(exc.cause, ControllerUnavailableError) else 409
        return HTTPException(status_code=status, detail={
            "message": str(exc), "code": "controller_unavailable" if status == 503 else "runtime_preflight_failed",
            "not_sent": True, "run_id": exc.event.run_id, "event_id": exc.event.event_id,
        })
    if isinstance(exc, ControllerUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, DeliveryBusy):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, DeliveryCancelled):
        return HTTPException(status_code=409, detail=str(exc))
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
