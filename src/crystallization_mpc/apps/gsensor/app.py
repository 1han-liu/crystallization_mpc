from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from crystallization_mpc.apps.central.experiments import CentralExperimentManager
from crystallization_mpc.apps.central.params import (
    load_param_meta,
    load_params,
    load_runtime_params,
)
from crystallization_mpc.apps.gsensor.alignment import (
    alignment_capabilities,
    parse_alignment_method,
)
from crystallization_mpc.apps.ui_mode import resolve_ui_mode, ui_mode_payload
from crystallization_mpc.apps.gsensor.DSCGR import DSCGR
from crystallization_mpc.apps.gsensor.detection.initialize_DSCGR import initialize_DSCGR
from crystallization_mpc.apps.gsensor.experiments import (
    ExperimentNotSelectedError,
    GsensorExperimentManager,
    GSENSOR_PROCESSING_SCHEMA_VERSION,
    validate_processing_schema_version,
)
from crystallization_mpc.apps.gsensor.growth_rate_processor import (
    FINAL_OVERLAY_FILENAME,
    LATEST_OVERLAY_FILENAME,
    GrowthRateFrameResult,
    GrowthRateProcessor,
)
from crystallization_mpc.apps.gsensor.image_watcher import (
    DetectedImage,
    ImageObservation,
    ImageProbe,
    image_identity_key,
    parse_image_sequence,
    scan_new_images,
    verify_image_readable,
)
from crystallization_mpc.apps.gsensor.initialization import (
    GsensorInitializationManager,
    list_supported_images,
)
from crystallization_mpc.apps.gsensor.status_publisher import (
    RabbitStatusPublisher,
    StatusPublisher,
)
from crystallization_mpc.apps.gsensor.telemetry import (
    GsensorMeasurementRecord,
    write_gsensor_measurement,
)
from crystallization_mpc.infra.influxdb.write import InfluxWriter
from crystallization_mpc.experiments import ExperimentStatus, TERMINAL_EXPERIMENT_STATUSES
from crystallization_mpc.experiments.registry import GSENSOR_INITIALIZATION_FILENAME
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.commands import (
    EXPERIMENT_SELECT_COMMAND,
    EXPERIMENT_START_COMMAND,
    EXPERIMENT_STOP_COMMAND,
    GSENSOR_DISABLE_COMMAND,
    GSENSOR_ENABLE_COMMAND,
    GROWTH_RATE_COMPLETED_MESSAGE,
    GROWTH_RATE_SAMPLE_MESSAGE,
    GROWTH_RATE_STATUS_MESSAGE,
    PARAMS_UPDATE_MESSAGE,
)
from crystallization_mpc.messaging.contracts import (
    ExperimentStartPayload,
    ExperimentStopPayload,
    GsensorActivationPayload,
    GrowthRateSamplePayload,
    GrowthRateStatus,
    GrowthRateStatusPayload,
)
from crystallization_mpc.messaging.idgen import next_seq
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for, route
from crystallization_mpc.messaging.schema import build_envelope, utc_ts

ROLE = "gsensor"
UI_DIR = Path(__file__).resolve().parent / "ui"
GSENSOR_IMGS_DIR = Path(__file__).resolve().parent / "imgs"
PROJECT_ROOT = UI_DIR.parents[4]
DEFAULT_PARAMS_PATH = PROJECT_ROOT / "params_default.yaml"
DEFAULT_RUNTIME_PARAMS_PATH = PROJECT_ROOT / "params_runtime.yaml"
DEFAULT_PARAM_META_PATH = PROJECT_ROOT / "param_meta.yaml"
DEFAULT_DSCGR_OUTPUT_ROOT = PROJECT_ROOT / ".runtime" / "dscgr_runs"
DEFAULT_EXPERIMENT_ROOT = PROJECT_ROOT / ".runtime" / "experiments"
logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _serialize_initial_line(uv_struct: Any) -> Dict[str, Any]:
    def point(value: Any) -> list[float]:
        return [float(item) for item in value][:2]

    return {
        "point1": point(uv_struct.t),
        "point2": point(uv_struct.e),
        "theta": float(uv_struct.theta_0),
        "rho": float(uv_struct.rho_0),
        "is_opposite": bool(uv_struct.is_opposite),
    }


class InitializationSessionRequest(BaseModel):
    session_id: str


class InitializationIsFullRequest(InitializationSessionRequest):
    is_full: bool


class InitializationPointRequest(InitializationSessionRequest):
    x: float
    y: float


class InitializationCornerRequest(InitializationSessionRequest):
    corner: str


class Initialization3DChoiceRequest(InitializationSessionRequest):
    choice: int


class InitializationConfirmRequest(InitializationSessionRequest):
    alignment_method: str | None = None


class InitializationResetRequest(InitializationSessionRequest):
    pass


class DscgrRunRequest(BaseModel):
    session_id: str | None = None


class SequenceResetRequest(BaseModel):
    run_id: str
    candidate_identity_key: str


class InitializationRestartRequest(BaseModel):
    run_id: str
    session_id: str | None = None
    control_revision: int = Field(ge=0, strict=True)


class AlignmentSelectionRequest(BaseModel):
    run_id: str
    session_id: str
    control_revision: int = Field(ge=0, strict=True)
    alignment_revision: int = Field(ge=0, strict=True)
    alignment_method: str


class GsensorService:
    def __init__(
        self,
        url: Optional[str] = None,
        exchange: Optional[str] = None,
        queue_name: Optional[str] = None,
        default_params_path: Optional[str | Path] = None,
        runtime_params_path: Optional[str | Path] = None,
        param_meta_path: Optional[str | Path] = None,
        dscgr_output_root_path: Optional[str | Path] = None,
        experiment_root_path: Optional[str | Path] = None,
        image_poll_interval_s: float | None = None,
        image_probe: ImageProbe | None = None,
        growth_rate_processor_factory: Callable[..., GrowthRateProcessor] | None = None,
        hough_debug_enabled: bool | None = None,
        status_publisher: StatusPublisher | None = None,
        status_publish_enabled: bool | None = None,
        sample_publisher: StatusPublisher | None = None,
        sample_publish_enabled: bool | None = None,
        measurement_writer: InfluxWriter | None = None,
        influx_enabled: bool | None = None,
    ) -> None:
        self.ui_mode = resolve_ui_mode()
        self.url = url or os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")
        self.exchange = exchange or os.getenv("RABBIT_EXCHANGE", EXCHANGE)
        self.queue_name = queue_name or os.getenv("RABBIT_QUEUE", QUEUES[ROLE])
        self.default_params_path = Path(
            default_params_path
            or os.getenv("PARAMS_DEFAULT_FILE", str(DEFAULT_PARAMS_PATH))
        )
        self.params_path = Path(
            runtime_params_path
            or os.getenv("PARAMS_FILE", str(DEFAULT_RUNTIME_PARAMS_PATH))
        )
        self.param_meta_path = Path(
            param_meta_path
            or os.getenv("PARAM_META_FILE", str(DEFAULT_PARAM_META_PATH))
        )
        self.dscgr_output_root_path = Path(
            dscgr_output_root_path
            or os.getenv("GSENSOR_DSCGR_OUTPUT_ROOT", str(DEFAULT_DSCGR_OUTPUT_ROOT))
        )
        self.experiment_root_path = Path(
            experiment_root_path
            or os.getenv("EXPERIMENT_ROOT", str(DEFAULT_EXPERIMENT_ROOT))
        )
        configured_poll_interval = (
            image_poll_interval_s
            if image_poll_interval_s is not None
            else float(os.getenv("GSENSOR_IMAGE_POLL_INTERVAL_S", "1.0"))
        )
        if configured_poll_interval <= 0:
            raise ValueError("GSENSOR_IMAGE_POLL_INTERVAL_S must be greater than zero.")
        self.image_poll_interval_s = float(configured_poll_interval)
        self.image_probe = image_probe
        self.growth_rate_processor_factory = (
            growth_rate_processor_factory or GrowthRateProcessor
        )
        self.hough_debug_enabled = (
            hough_debug_enabled
            if hough_debug_enabled is not None
            else _env_flag("GSENSOR_HOUGH_DEBUG_ENABLED", False)
        )
        publisher_enabled = (
            status_publish_enabled
            if status_publish_enabled is not None
            else _env_flag("GSENSOR_STATUS_PUBLISH_ENABLED", False)
        )
        self.status_publisher = status_publisher
        if self.status_publisher is None and publisher_enabled:
            self.status_publisher = RabbitStatusPublisher(
                url=self.url,
                exchange=self.exchange,
                destination_queue=QUEUES["central"],
                destination_role="central",
            )
        sample_enabled = (
            sample_publish_enabled
            if sample_publish_enabled is not None
            else _env_flag("GSENSOR_SAMPLE_PUBLISH_ENABLED", False)
        )
        self.sample_publisher = sample_publisher
        if self.sample_publisher is None and sample_enabled:
            self.sample_publisher = RabbitStatusPublisher(
                url=self.url,
                exchange=self.exchange,
                destination_queue=QUEUES["controller"],
                destination_role="controller",
            )
        persistence_enabled = (
            influx_enabled
            if influx_enabled is not None
            else _env_flag("GSENSOR_INFLUX_ENABLED", False)
        )
        self.measurement_writer = measurement_writer
        self.influx_initialization_error: str | None = None
        if self.measurement_writer is None and persistence_enabled:
            try:
                self.measurement_writer = InfluxWriter()
            except Exception as exc:
                self.influx_initialization_error = str(exc)
                logger.warning("Could not initialize Gsensor InfluxDB writer: %s", exc)
        self.params: Dict[str, Any] = {}
        self.active = False
        self.initialized = False
        self.initialization_status = "not_initialized"
        self.initialized_at: str | None = None
        self.last_message: Dict[str, Any] | None = None
        self.last_command_message: Dict[str, Any] | None = None
        self.last_params_message: Dict[str, Any] | None = None
        self.last_measurement_step_at: str | None = None
        self.measurement_step_count = 0
        self.measurement_valid_frame_count = 0
        self.measurement_invalid_frame_count = 0
        self.initialization_generation = 0
        self.initialization_history: list[Dict[str, Any]] = []
        self.reinitialization_history: list[Dict[str, Any]] = []
        self.reinitialization_error: str | None = None
        self._reinitialization_pending = False
        self.last_dscgr_result: Dict[str, Any] | None = None
        self.baseline: Dict[str, Any] | None = None
        self.uv_struct_list: list[Any] | None = None
        self.kernel: Any | None = None
        self.growth_rate_processor: GrowthRateProcessor | None = None
        self.last_growth_rate_result: Dict[str, Any] | None = None
        self.last_processing_error: str | None = None
        self.latest_overlay_path: str | None = None
        self.final_overlay_path: str | None = None
        self.last_status_message: Dict[str, Any] | None = None
        self.last_status_publish_error: str | None = None
        self.last_controller_status_message: Dict[str, Any] | None = None
        self.last_controller_status_publish_error: str | None = None
        self.last_sample_message: Dict[str, Any] | None = None
        self.last_sample_publish_error: str | None = None
        self.sample_publish_success_count = 0
        self.sample_publish_failure_count = 0
        self.last_influx_write_at: str | None = None
        self.last_influx_error: str | None = self.influx_initialization_error
        self.influx_write_success_count = 0
        self.influx_write_failure_count = 0
        self.initialization = GsensorInitializationManager()
        self.experiments = GsensorExperimentManager(self.experiment_root_path)
        self.current_experiment: Dict[str, Any] | None = None
        self.experiment_selection_status = "not_selected"
        self.experiment_selection_error: str | None = None
        self.last_experiment_message: Dict[str, Any] | None = None
        self.experiment_lifecycle_status = "not_started"
        self.experiment_lifecycle_error: str | None = None
        self.experiment_started_at: str | None = None
        self.gsensor_enabled = False
        self.gsensor_resume_status = GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
        self.gsensor_control_revision = 0
        self.gsensor_control_event_id: str | None = None
        self.gsensor_last_command_enabled: bool | None = None
        self.gsensor_last_command_error: str | None = None
        self.last_lifecycle_command_error: str | None = None
        self.experiment_parameter_version: int | None = None
        self.experiment_params: Dict[str, Any] | None = None
        self.confirmed_alignment_method: str | None = None
        self.alignment_confirmed_at: str | None = None
        self.alignment_revision = 0
        self.alignment_effective_after_frame_seq = 0
        self.alignment_changes: list[Dict[str, Any]] = []
        self.alignment_change_error: str | None = None
        self._alignment_change_pending = False
        self.last_lifecycle_message: Dict[str, Any] | None = None
        # Values are revision identities (name + mtime_ns + size), not filenames.
        self.processed_image_files: set[str] = set()
        self.processed_image_records: Dict[str, Dict[str, Any]] = {}
        self.image_observations: Dict[str, ImageObservation] = {}
        self.latest_discovered_image: Dict[str, Any] | None = None
        self.latest_ready_image: Dict[str, Any] | None = None
        self.sequence_watermark: int | None = None
        self.sequence_watermark_record: Dict[str, Any] | None = None
        self.sequence_epoch = 0
        self.missing_sequence_ranges: list[Dict[str, Any]] = []
        self.late_image_count = 0
        self.last_late_image: Dict[str, Any] | None = None
        self.sequence_reset_candidate: Dict[str, Any] | None = None
        self.ignored_image_files: set[str] = set()
        self.image_discovery_revision = 0
        self.last_detected_image: str | None = None
        self.file_modified_at: str | None = None
        self.detected_at: str | None = None
        self.pending_image_count = 0
        self.last_image_scan_at: str | None = None
        self.image_scan_status = "stopped"
        self.image_scan_error: str | None = None
        self.recovery_status = "not_attempted"
        self.recovery_error: str | None = None
        self.processing_state_path: str | None = None
        self._consumer_thread: threading.Thread | None = None
        self._measurement_thread: threading.Thread | None = None
        self._measurement_stop = threading.Event()
        self._lock = threading.RLock()
        self._control_lock = threading.RLock()
        self._publication_lock = threading.RLock()
        self._image_scan_lock = threading.RLock()
        try:
            self.current_experiment = self.experiments.current()
        except Exception as exc:
            self.experiment_selection_status = "error"
            self.experiment_selection_error = str(exc)
            logger.exception("Could not restore Gsensor experiment selection.")
        else:
            if self.current_experiment is not None:
                self.experiment_selection_status = "selected"
                self.experiment_lifecycle_status = "selected"
                try:
                    self._restore_processing_state_locked()
                except Exception as exc:
                    self.recovery_status = "error"
                    self.recovery_error = str(exc)
                    self.experiment_lifecycle_status = GrowthRateStatus.ERROR.value
                    self.experiment_lifecycle_error = (
                        f"Gsensor recovery failed: {exc}"
                    )
                    logger.exception("Could not restore Gsensor processing state.")

    def start(self) -> None:
        with self._lock:
            restored_disabled = self.experiment_lifecycle_status == GrowthRateStatus.DISABLED.value
            if restored_disabled:
                self._persist_activation_state_locked()
            should_resume = self.gsensor_enabled and self.experiment_lifecycle_status in {
                GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value,
                GrowthRateStatus.INITIALIZING.value,
                GrowthRateStatus.BASELINE_READY.value,
                GrowthRateStatus.MEASURING.value,
            }
        if should_resume:
            self.start_image_scanning()
        elif restored_disabled:
            self._publish_growth_rate_status(GrowthRateStatus.DISABLED)
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._consumer_thread = threading.Thread(
            target=self._consume_forever,
            name="gsensor-rabbitmq-consumer",
            daemon=True,
        )
        self._consumer_thread.start()

    def close(self) -> None:
        self.stop_image_scanning()
        for publisher in (self.status_publisher, self.sample_publisher):
            close = getattr(publisher, "close", None)
            if callable(close):
                close()
        close_writer = getattr(self.measurement_writer, "close", None)
        if callable(close_writer):
            close_writer()

    def _consume_forever(self) -> None:
        while True:
            try:
                start_consumer(
                    url=self.url,
                    exchange=self.exchange,
                    queue_name=self.queue_name,
                    binding_keys=bindings_for(ROLE),
                    on_message=self.on_message,
                )
            except Exception:
                logger.exception("Gsensor RabbitMQ consumer stopped; retrying.")
                time.sleep(5)

    def on_message(self, msg: Dict[str, Any]) -> None:
        with self._lock:
            self.last_message = msg
        if msg.get("msg_type") == "params" and msg.get("name") == PARAMS_UPDATE_MESSAGE:
            params = msg.get("payload", {}).get("params", {})
            if isinstance(params, dict):
                with self._lock:
                    self.params.update(params)
                    self.last_params_message = msg
            return

        if msg.get("msg_type") == "command":
            with self._lock:
                self.last_command_message = msg
            name = msg.get("name")
            if name in {GSENSOR_ENABLE_COMMAND, GSENSOR_DISABLE_COMMAND}:
                try:
                    self._validate_central_lifecycle_message(msg)
                    self.set_gsensor_enabled(
                        msg.get("payload", {}), enabled=name == GSENSOR_ENABLE_COMMAND
                    )
                except Exception as exc:
                    with self._lock:
                        self.gsensor_last_command_error = str(exc)
                    logger.warning("Rejected %s command: %s", name, exc)
            elif name == EXPERIMENT_START_COMMAND:
                try:
                    self.start_experiment(msg)
                except Exception as exc:
                    with self._lock:
                        self.last_lifecycle_command_error = str(exc)
                        self.last_lifecycle_message = msg
                    logger.warning("Rejected experiment.start command: %s", exc)
            elif name == EXPERIMENT_STOP_COMMAND:
                try:
                    self.stop_experiment(msg)
                except Exception as exc:
                    with self._lock:
                        self.last_lifecycle_command_error = str(exc)
                        self.last_lifecycle_message = msg
                    logger.warning("Rejected experiment.stop command: %s", exc)
            elif name == EXPERIMENT_SELECT_COMMAND:
                try:
                    if msg.get("src") != "central" or msg.get("dst") != ROLE:
                        raise ValueError(
                            "experiment.select must be sent from central to gsensor."
                        )
                    self.select_experiment(msg.get("payload", {}), message=msg)
                except Exception as exc:
                    with self._lock:
                        self.experiment_selection_status = "rejected"
                        self.experiment_selection_error = str(exc)
                        self.last_experiment_message = msg
                    logger.warning("Rejected experiment.select command: %s", exc)

    def select_experiment(
        self,
        payload: Dict[str, Any],
        *,
        message: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        with self._control_lock, self._lock:
            selection, changed = self.experiments.select(
                payload,
                running=self.active or self._experiment_in_progress_locked() or self._measurement_running(),
            )
            if changed:
                self._reset_experiment_runtime_locked()
            self.current_experiment = selection
            self.experiment_selection_status = "selected"
            self.experiment_selection_error = None
            self.last_experiment_message = message
            if changed:
                self.experiment_lifecycle_status = "selected"
            return dict(selection)

    def _reset_image_discovery_locked(self) -> None:
        self.processed_image_files.clear()
        self.processed_image_records.clear()
        self.image_observations.clear()
        self.latest_discovered_image = None
        self.latest_ready_image = None
        self.sequence_watermark = None
        self.sequence_watermark_record = None
        self.sequence_epoch = 0
        self.missing_sequence_ranges.clear()
        self.late_image_count = 0
        self.last_late_image = None
        self.sequence_reset_candidate = None
        self.ignored_image_files.clear()
        self.image_discovery_revision += 1
        self.last_detected_image = None
        self.file_modified_at = None
        self.detected_at = None
        self.pending_image_count = 0

    def _reset_experiment_runtime_locked(self) -> None:
        self.measurement_valid_frame_count = 0
        self.measurement_invalid_frame_count = 0
        self.initialization_generation = 0
        self.initialization_history = []
        self.reinitialization_history = []
        self.reinitialization_error = None
        self._reinitialization_pending = False
        self.gsensor_enabled = False
        self.gsensor_resume_status = GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
        self.gsensor_control_revision = 0
        self.gsensor_control_event_id = None
        self.gsensor_last_command_enabled = None
        self.gsensor_last_command_error = None
        self.last_lifecycle_command_error = None
        self.initialized = False
        self.initialization_status = "not_initialized"
        self.initialized_at = None
        self.initialization = GsensorInitializationManager()
        self.last_measurement_step_at = None
        self.measurement_step_count = 0
        self.last_dscgr_result = None
        self.baseline = None
        self.uv_struct_list = None
        self.kernel = None
        self.growth_rate_processor = None
        self.last_growth_rate_result = None
        self.last_processing_error = None
        self.latest_overlay_path = None
        self.final_overlay_path = None
        self.last_status_message = None
        self.last_status_publish_error = None
        self.last_controller_status_message = None
        self.last_controller_status_publish_error = None
        self.last_sample_message = None
        self.last_sample_publish_error = None
        self.sample_publish_success_count = 0
        self.sample_publish_failure_count = 0
        self.last_influx_write_at = None
        self.last_influx_error = self.influx_initialization_error
        self.influx_write_success_count = 0
        self.influx_write_failure_count = 0
        self._measurement_stop.clear()
        self._measurement_thread = None
        self._reset_image_discovery_locked()
        self.last_image_scan_at = None
        self.image_scan_status = "stopped"
        self.image_scan_error = None
        self.experiment_started_at = None
        self.experiment_parameter_version = None
        self.experiment_params = None
        self.confirmed_alignment_method = None
        self.alignment_confirmed_at = None
        self.alignment_revision = 0
        self.alignment_effective_after_frame_seq = 0
        self.alignment_changes = []
        self.alignment_change_error = None
        self._alignment_change_pending = False
        self.experiment_lifecycle_error = None
        self.last_lifecycle_message = None
        self.recovery_status = "not_attempted"
        self.recovery_error = None
        self.processing_state_path = None

    def _processing_state_document_locked(self) -> Dict[str, Any]:
        if self.current_experiment is None:
            raise ExperimentNotSelectedError("No experiment is selected.")
        processor_state = None
        if self.growth_rate_processor is not None:
            export_state = getattr(self.growth_rate_processor, "export_state", None)
            if callable(export_state):
                processor_state = export_state()
        initialization_payload = self.initialization.payload()
        if initialization_payload.get("session_id") is None:
            initialization_payload = None
        return {
            "schema_version": GSENSOR_PROCESSING_SCHEMA_VERSION,
            "run_id": self.current_experiment["run_id"],
            "updated_at": utc_ts(),
            "lifecycle_status": self.experiment_lifecycle_status,
            "lifecycle_error": self.experiment_lifecycle_error,
            "experiment_started_at": self.experiment_started_at,
            "gsensor_activation": self._activation_state_locked(),
            "measurement_counters": {
                "frame_seq": self.measurement_step_count,
                "valid": self.measurement_valid_frame_count,
                "invalid": self.measurement_invalid_frame_count,
            },
            "reinitialization": {
                "generation": self.initialization_generation,
                "initializations": self.initialization_history,
                "history": self.reinitialization_history,
            },
            "parameter_version": self.experiment_parameter_version,
            "algorithm_params": self.experiment_params,
            "alignment_configuration": {
                key: value
                for key, value in self._alignment_configuration_locked().items()
                if key not in {"can_select", "can_switch", "in_progress", "last_error", "warmup_pending"}
            },
            "alignment_changes": self.alignment_changes,
            "initialization": initialization_payload,
            "initialized_at": self.initialized_at,
            "baseline": self.baseline,
            "processed_images": list(self.processed_image_records.values()),
            "image_discovery": {
                "latest_ready": self.latest_ready_image,
                "sequence_watermark": self.sequence_watermark,
                "sequence_watermark_record": self.sequence_watermark_record,
                "sequence_epoch": self.sequence_epoch,
                "missing_sequence_ranges": self.missing_sequence_ranges,
                "late_image_count": self.late_image_count,
                "last_late_image": self.last_late_image,
                "reset_candidate": self.sequence_reset_candidate,
                "ignored_images": sorted(self.ignored_image_files),
            },
            "processor": processor_state,
            "last_growth_rate_result": self.last_growth_rate_result,
            "latest_overlay_path": self.latest_overlay_path,
            "final_overlay_path": self.final_overlay_path,
        }

    def _persist_processing_state_locked(self) -> None:
        if self.current_experiment is None:
            return
        path = self.experiments.save_processing_state(
            self.current_experiment["run_id"],
            self._processing_state_document_locked(),
        )
        self.processing_state_path = str(path)

    def _restore_processing_state_locked(self) -> None:
        if self.current_experiment is None:
            return
        run_id = str(self.current_experiment["run_id"])
        state = self.experiments.load_processing_state(run_id)
        if state is None:
            self.recovery_status = "not_available"
            return
        validate_processing_schema_version(state.get("schema_version"))

        lifecycle_status = str(state.get("lifecycle_status") or "selected")
        allowed_statuses = {
            "selected",
            GrowthRateStatus.DISABLED.value,
            GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value,
            GrowthRateStatus.INITIALIZING.value,
            GrowthRateStatus.BASELINE_READY.value,
            GrowthRateStatus.MEASURING.value,
            GrowthRateStatus.STOPPING.value,
            GrowthRateStatus.STOPPED.value,
            GrowthRateStatus.COMPLETED.value,
            GrowthRateStatus.ERROR.value,
        }
        if lifecycle_status not in allowed_statuses:
            raise ValueError(
                f"Unsupported persisted Gsensor lifecycle status: {lifecycle_status}."
            )
        records = state.get("processed_images") or []
        if not isinstance(records, list):
            raise ValueError("Persisted processed_images must be a list.")
        restored_records: Dict[str, Dict[str, Any]] = {}
        for item in records:
            if not isinstance(item, dict):
                raise ValueError("Persisted image identity must be an object.")
            identity_key = str(item.get("identity_key") or "").strip()
            image_name = str(item.get("image_name") or "").strip()
            if not identity_key or not image_name:
                raise ValueError("Persisted image identity is incomplete.")
            restored_records[identity_key] = dict(item)

        self.processed_image_records = restored_records
        self.processed_image_files = set(restored_records)
        self.image_observations = {}
        self.latest_discovered_image = None
        discovery = state.get("image_discovery")
        if not isinstance(discovery, dict):
            discovery = {}
        latest_ready = discovery.get("latest_ready")
        self.latest_ready_image = (
            dict(latest_ready) if isinstance(latest_ready, dict) else None
        )
        raw_watermark = discovery.get("sequence_watermark")
        self.sequence_watermark = (
            int(raw_watermark) if type(raw_watermark) is int else None
        )
        watermark_record = discovery.get("sequence_watermark_record")
        self.sequence_watermark_record = (
            dict(watermark_record) if isinstance(watermark_record, dict) else None
        )
        raw_epoch = discovery.get("sequence_epoch", 0)
        self.sequence_epoch = max(0, int(raw_epoch)) if type(raw_epoch) is int else 0
        raw_missing = discovery.get("missing_sequence_ranges")
        self.missing_sequence_ranges = (
            [dict(item) for item in raw_missing if isinstance(item, dict)]
            if isinstance(raw_missing, list)
            else []
        )
        raw_late_count = discovery.get("late_image_count", 0)
        self.late_image_count = (
            max(0, int(raw_late_count)) if type(raw_late_count) is int else 0
        )
        last_late = discovery.get("last_late_image")
        self.last_late_image = dict(last_late) if isinstance(last_late, dict) else None
        reset_candidate = discovery.get("reset_candidate")
        self.sequence_reset_candidate = (
            dict(reset_candidate) if isinstance(reset_candidate, dict) else None
        )
        ignored_images = discovery.get("ignored_images") or []
        if not isinstance(ignored_images, list) or any(not isinstance(item, str) for item in ignored_images):
            raise ValueError("Persisted ignored_images must be a list of revision identities.")
        self.ignored_image_files = set(ignored_images)
        sequenced_records = []
        for record in restored_records.values():
            raw_sequence = record.get("source_sequence", record.get("sequence_number"))
            sequence = (
                int(raw_sequence)
                if type(raw_sequence) is int
                else parse_image_sequence(str(record["image_name"]))
            )
            if sequence is not None and record.get("disposition") != "late":
                sequenced_records.append((sequence, record))
        if not discovery and sequenced_records:
            self.sequence_watermark = max(item[0] for item in sequenced_records)
            self.sequence_watermark_record = dict(max(sequenced_records, key=lambda item: item[0])[1])
        # File readiness must be checked again after a service restart.
        self.latest_ready_image = None
        self.image_discovery_revision += 1
        self.experiment_lifecycle_status = lifecycle_status
        self.experiment_lifecycle_error = state.get("lifecycle_error")
        self.experiment_started_at = state.get("experiment_started_at")
        activation = state.get("gsensor_activation") or {}
        if not isinstance(activation, dict):
            raise ValueError("Persisted gsensor_activation must be an object.")
        revision = activation.get("control_revision", 0)
        if type(revision) is not int or revision < 0:
            raise ValueError("Persisted Gsensor control_revision must be a nonnegative integer.")
        self.gsensor_control_revision = revision
        self.gsensor_control_event_id = activation.get("control_event_id")
        self.gsensor_last_command_enabled = activation.get("last_command_enabled")
        resume_status = activation.get("resume_status", lifecycle_status)
        resumable = {
            GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value,
            GrowthRateStatus.INITIALIZING.value,
            GrowthRateStatus.BASELINE_READY.value,
            GrowthRateStatus.MEASURING.value,
        }
        # A process restart never grants permission to operate the sensor. Old
        # snapshots also restore paused; the operator must send a new revision.
        if lifecycle_status in resumable:
            resume_status = lifecycle_status
            self.experiment_lifecycle_status = GrowthRateStatus.DISABLED.value
        self.gsensor_resume_status = (
            resume_status if resume_status in resumable
            else GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
        )
        self.gsensor_enabled = False
        self._measurement_stop.set()
        if self.experiment_lifecycle_status == GrowthRateStatus.DISABLED.value:
            self.image_scan_status = "disabled"
        parameter_version = state.get("parameter_version")
        self.experiment_parameter_version = (
            int(parameter_version) if parameter_version is not None else None
        )
        algorithm_params = state.get("algorithm_params")
        self.experiment_params = (
            dict(algorithm_params) if isinstance(algorithm_params, dict) else None
        )
        self.baseline = state.get("baseline")
        self.initialized_at = state.get("initialized_at")
        self.last_growth_rate_result = state.get("last_growth_rate_result")
        self.latest_overlay_path = state.get("latest_overlay_path")
        self.final_overlay_path = state.get("final_overlay_path")
        counters = state.get("measurement_counters") or {}
        old_processor = state.get("processor") or {}
        self.measurement_step_count = int(counters.get("frame_seq", (
            (self.last_growth_rate_result or {}).get("frame_seq")
            or old_processor.get("frame_seq", 0)
        )))
        self.measurement_valid_frame_count = int(counters.get("valid", old_processor.get("valid_frame_count", 0)))
        self.measurement_invalid_frame_count = int(counters.get("invalid", old_processor.get("invalid_frame_count", 0)))
        reinitialization = state.get("reinitialization") or {}
        self.initialization_generation = int(reinitialization.get("generation", 0))
        if min(self.initialization_generation, self.measurement_step_count,
               self.measurement_valid_frame_count, self.measurement_invalid_frame_count) < 0:
            raise ValueError("Gsensor generation and measurement counters cannot be negative.")
        self.initialization_history = list(reinitialization.get("initializations") or [])
        self.reinitialization_history = list(reinitialization.get("history") or [])
        if self.last_growth_rate_result:
            self.last_detected_image = self.last_growth_rate_result.get("image_name")
            self.last_measurement_step_at = self.last_growth_rate_result.get(
                "processed_at"
            )

        initialization_payload = state.get("initialization")
        if isinstance(initialization_payload, dict):
            self.initialization.restore(initialization_payload)
            self.initialization_status = str(
                initialization_payload.get("status") or "not_initialized"
            )

        processor_state = state.get("processor")
        self._restore_alignment_configuration_locked(
            state.get("alignment_configuration"), has_processor=processor_state is not None
        )
        self.alignment_changes = list(state.get("alignment_changes") or [])
        if processor_state is not None:
            if not isinstance(initialization_payload, dict):
                raise ValueError(
                    "Processor recovery requires the initialization snapshot."
                )
            if not isinstance(self.experiment_params, dict):
                raise ValueError("Processor recovery requires experiment parameters.")
            session_id = str(initialization_payload.get("session_id") or "")
            uv_struct_list, kernel = initialize_DSCGR(
                self.initialization,
                session_id=session_id,
            )
            output_directory = self._measurement_output_directory_locked()
            latest_overlay_path = output_directory / LATEST_OVERLAY_FILENAME
            final_overlay_path = output_directory / FINAL_OVERLAY_FILENAME
            debug_directory = (
                self.dscgr_output_root_path / run_id / "hough_debug"
                if self.hough_debug_enabled
                else None
            )
            processor = self.growth_rate_processor_factory(
                run_id=run_id,
                params=self.experiment_params,
                uv_struct_list=uv_struct_list,
                kernel=kernel,
                latest_overlay_path=latest_overlay_path,
                final_overlay_path=final_overlay_path,
                debug_directory=debug_directory,
                initial_image_path=initialization_payload["selected_image"],
                alignment_method=self.confirmed_alignment_method,
            )
            restore_state = getattr(processor, "restore_state", None)
            if not callable(restore_state):
                raise ValueError("Growth-rate processor does not support recovery.")
            restore_state(processor_state)
            self.growth_rate_processor = processor
            self.uv_struct_list = processor.uv_structs
            self.kernel = kernel
            self.initialized = True
            if not self.initialization_history:
                self.initialization_history = [{
                    "generation": 0,
                    "session_id": initialization_payload.get("session_id"),
                    "initialization_file": GSENSOR_INITIALIZATION_FILENAME,
                    "completed_at": self.initialized_at,
                }]
            self.latest_overlay_path = self.latest_overlay_path or (
                str(latest_overlay_path) if latest_overlay_path.is_file() else None
            )
            self.final_overlay_path = (
                str(final_overlay_path) if final_overlay_path.is_file() else None
            )

        visible_records = [
            record
            for record in restored_records.values()
            if record.get("disposition") not in {"backlog", "late"}
        ]
        if visible_records:
            last_record = visible_records[-1]
            self.last_detected_image = str(last_record["image_name"])
            self.file_modified_at = last_record.get("file_modified_at")
            self.detected_at = last_record.get("detected_at")
        self.processing_state_path = str(
            self.experiments.processing_state_path(run_id)
        )
        self.recovery_status = "restored"
        self.recovery_error = None

    def start_experiment(self, message: Dict[str, Any]) -> None:
        with self._control_lock:
            self._start_experiment(message)

    def _start_experiment(self, message: Dict[str, Any]) -> None:
        self._validate_central_lifecycle_message(message)
        payload = ExperimentStartPayload.from_mapping(message.get("payload", {}))
        duplicate_status: GrowthRateStatus | None = None
        with self._publication_lock, self._lock:
            selection = self.experiments.require_current()
            if payload.run_id != selection["run_id"]:
                raise ValueError("experiment.start run_id does not match the selected experiment.")
            if payload.image_directory != selection["image_directory"]:
                raise ValueError(
                    "experiment.start image_directory does not match the selected experiment."
                )
            active_statuses = {
                GrowthRateStatus.DISABLED,
                GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE,
                GrowthRateStatus.INITIALIZING,
                GrowthRateStatus.BASELINE_READY,
                GrowthRateStatus.MEASURING,
            }
            current_status = self.experiment_lifecycle_status
            if (
                self.experiment_started_at == payload.started_at
                and self.experiment_parameter_version == payload.parameter_version
                and current_status in {status.value for status in active_statuses}
            ):
                # RabbitMQ delivery can be retried after Central has persisted
                # STARTING. Treat the same command as idempotent so an active
                # initialization or measurement is never reset.
                self.last_lifecycle_message = message
                duplicate_status = GrowthRateStatus(current_status)
            if duplicate_status is None:
                if self._experiment_in_progress_locked():
                    raise ValueError("An experiment is already in progress; a different start cannot replace it.")
                if current_status in {
                    GrowthRateStatus.STOPPED.value, GrowthRateStatus.COMPLETED.value,
                } or self.experiments.registry.get(payload.run_id).status in TERMINAL_EXPERIMENT_STATUSES:
                    raise ValueError("A finished experiment cannot be restarted.")
                self.gsensor_enabled = False
                self.gsensor_resume_status = GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
                self.gsensor_control_revision = 0
                self.gsensor_control_event_id = None
                self.gsensor_last_command_enabled = None
                self.gsensor_last_command_error = None
                self.active = False
                self._measurement_stop.set()
                self.initialized = False
                self.initialization_status = "not_initialized"
                self.initialized_at = None
                self.initialization = GsensorInitializationManager()
                self.baseline = None
                self.uv_struct_list = None
                self.kernel = None
                self.growth_rate_processor = None
                self.last_growth_rate_result = None
                self.last_processing_error = None
                self.latest_overlay_path = None
                self.final_overlay_path = None
                self._reset_image_discovery_locked()
                self.measurement_step_count = 0
                self.measurement_valid_frame_count = 0
                self.measurement_invalid_frame_count = 0
                self.initialization_generation = 0
                self.initialization_history = []
                self.reinitialization_history = []
                self.reinitialization_error = None
                self.last_measurement_step_at = None
                self.experiment_started_at = payload.started_at
                self.experiment_parameter_version = payload.parameter_version
                self.experiment_params = self.current_params()
                self.confirmed_alignment_method = None
                self.alignment_confirmed_at = None
                self.alignment_revision = 0
                self.alignment_effective_after_frame_seq = 0
                self.alignment_changes = []
                self.alignment_change_error = None
                self._alignment_change_pending = False
                self.experiment_lifecycle_status = (
                    GrowthRateStatus.DISABLED.value
                )
                self.image_scan_status = "disabled"
                self.experiment_lifecycle_error = None
                self.last_lifecycle_message = message
                self.recovery_status = "not_needed"
                self.recovery_error = None
                self._persist_processing_state_locked()
        if duplicate_status is not None:
            self._publish_growth_rate_status(duplicate_status)
            return
        self.last_lifecycle_command_error = None
        self._publish_growth_rate_status(GrowthRateStatus.DISABLED)

    def _activation_state_locked(self) -> Dict[str, Any]:
        return {
            "enabled": self.gsensor_enabled,
            "resume_status": (
                self.experiment_lifecycle_status if self.gsensor_enabled
                else self.gsensor_resume_status
            ),
            "control_revision": self.gsensor_control_revision,
            "control_event_id": self.gsensor_control_event_id,
            "last_command_enabled": self.gsensor_last_command_enabled,
            "last_command_error": self.gsensor_last_command_error,
        }

    def _persist_activation_state_locked(self) -> None:
        # A disable may interrupt processor.process(). Update only the gate on
        # disk until that frame commits its complete processor snapshot.
        if self.current_experiment is None:
            return
        run_id = self.current_experiment["run_id"]
        state = self.experiments.load_processing_state(run_id)
        if state is None:
            self._persist_processing_state_locked()
            return
        state.update({
            "updated_at": utc_ts(),
            "lifecycle_status": self.experiment_lifecycle_status,
            "gsensor_activation": self._activation_state_locked(),
        })
        self.processing_state_path = str(self.experiments.save_processing_state(run_id, state))

    def set_gsensor_enabled(
        self,
        payload: Dict[str, Any] | GsensorActivationPayload,
        *,
        enabled: bool,
    ) -> Dict[str, Any]:
        """Pause/resume this sensor without ending the experiment."""
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean.")
        command = (
            payload if isinstance(payload, GsensorActivationPayload)
            else GsensorActivationPayload.from_mapping(payload)
        )
        with self._control_lock:
            with self._publication_lock, self._lock:
                selection = self.experiments.require_current()
                if command.run_id != selection["run_id"]:
                    raise ValueError("Gsensor command run_id does not match the selected experiment.")
                if not self.experiment_started_at or command.experiment_started_at != self.experiment_started_at:
                    raise ValueError("Gsensor command does not match the current experiment start.")
                if (
                    not self._experiment_in_progress_locked()
                    or self.experiment_lifecycle_status == GrowthRateStatus.STOPPING.value
                    or self.experiments.registry.get(command.run_id).status in TERMINAL_EXPERIMENT_STATUSES
                ):
                    raise ValueError("Gsensor can only be enabled or disabled during an active experiment.")
                if command.revision < self.gsensor_control_revision:
                    raise ValueError("Gsensor command is stale (control revision has advanced).")
                duplicate = command.revision == self.gsensor_control_revision
                if duplicate and (
                    command.event_id != self.gsensor_control_event_id
                    or enabled != self.gsensor_last_command_enabled
                ):
                    raise ValueError("Gsensor command conflicts with the accepted control revision.")
                if not duplicate:
                    if command.event_id == self.gsensor_control_event_id:
                        raise ValueError("A new Gsensor control revision requires a new event_id.")
                    # Never clear the stop flag while the previous poller owns
                    # the processor. Retry after it exits instead of forking it.
                    if enabled and self._measurement_stop.is_set() and self._measurement_running():
                        raise ValueError("The previous Gsensor worker is still stopping; retry after it stops.")
                    previous = {
                        key: getattr(self, key) for key in (
                            "gsensor_enabled", "gsensor_resume_status", "experiment_lifecycle_status",
                            "gsensor_control_revision", "gsensor_control_event_id",
                            "gsensor_last_command_enabled", "image_discovery_revision", "active",
                        )
                    }
                    if not enabled and self.gsensor_enabled:
                        self.gsensor_resume_status = self.experiment_lifecycle_status
                    if enabled and not self.gsensor_enabled:
                        self.experiment_lifecycle_status = self.gsensor_resume_status
                    elif not enabled:
                        self.experiment_lifecycle_status = GrowthRateStatus.DISABLED.value
                    self.gsensor_enabled = enabled
                    self.gsensor_control_revision = command.revision
                    self.gsensor_control_event_id = command.event_id
                    self.gsensor_last_command_enabled = enabled
                    self.gsensor_last_command_error = None
                    self.image_discovery_revision += 1
                    if not enabled:
                        self.active = False
                        self._measurement_stop.set()
                    try:
                        self._persist_activation_state_locked()
                    except Exception as exc:
                        if enabled:
                            for key, value in previous.items():
                                setattr(self, key, value)
                        else:
                            # Stay closed even when disk is unavailable, but do
                            # not acknowledge an uncommitted control revision.
                            for key in (
                                "gsensor_control_revision", "gsensor_control_event_id",
                                "gsensor_last_command_enabled",
                            ):
                                setattr(self, key, previous[key])
                        self.gsensor_last_command_error = str(exc)
                        raise
                # Retried enable after recovery acknowledges the old command,
                # without granting fresh permission to run.
                should_run = self.gsensor_enabled
            # Announce the activation boundary before starting or draining a
            # worker, so Controller cannot continue using its previous G cache.
            self._publish_transition_availability()
            try:
                if should_run:
                    self.start_image_scanning()
                else:
                    self.stop_image_scanning()
                    with self._image_scan_lock, self._lock:
                        self.image_scan_status = "disabled"
                        self._persist_processing_state_locked()
            except Exception as exc:
                with self._publication_lock, self._lock:
                    if self.gsensor_enabled:
                        self.gsensor_resume_status = self.experiment_lifecycle_status
                    self.gsensor_enabled = False
                    self.active = False
                    self._measurement_stop.set()
                    self.experiment_lifecycle_status = GrowthRateStatus.DISABLED.value
                    self.gsensor_last_command_error = str(exc)
                    self._persist_activation_state_locked()
                self._publish_growth_rate_status(GrowthRateStatus.DISABLED)
                raise
            with self._lock:
                self.gsensor_last_command_error = None
                current_status = GrowthRateStatus(self.experiment_lifecycle_status)
                activation = self._activation_state_locked()
                current_error = self.experiment_lifecycle_error
            self._publish_growth_rate_status(
                current_status,
                error=current_error if current_status == GrowthRateStatus.ERROR else None,
            )
            return activation

    def stop_experiment(self, message: Dict[str, Any]) -> None:
        with self._control_lock:
            self._stop_experiment(message)

    def _stop_experiment(self, message: Dict[str, Any]) -> None:
        self._validate_central_lifecycle_message(message)
        payload = ExperimentStopPayload.from_mapping(message.get("payload", {}))
        duplicate_status: GrowthRateStatus | None = None
        with self._publication_lock, self._lock:
            selection = self.experiments.require_current()
            if payload.run_id != selection["run_id"]:
                raise ValueError("experiment.stop run_id does not match the selected experiment.")
            self.gsensor_enabled = False
            self.active = False
            self._measurement_stop.set()
            self.image_discovery_revision += 1
            self.last_lifecycle_command_error = None
            current_status = self.experiment_lifecycle_status
            if current_status in {
                GrowthRateStatus.STOPPED.value,
                GrowthRateStatus.COMPLETED.value,
            }:
                duplicate_status = GrowthRateStatus(current_status)
                self.last_lifecycle_message = message
            else:
                self.experiment_lifecycle_status = GrowthRateStatus.STOPPING.value
                self.experiment_lifecycle_error = None
                self.last_lifecycle_message = message
                self._persist_activation_state_locked()
        if duplicate_status == GrowthRateStatus.COMPLETED:
            self._publish_growth_rate_status(
                GrowthRateStatus.COMPLETED,
                message_name=GROWTH_RATE_COMPLETED_MESSAGE,
            )
            return
        if duplicate_status == GrowthRateStatus.STOPPED:
            with self._lock:
                self.experiment_lifecycle_status = GrowthRateStatus.COMPLETED.value
                self._persist_processing_state_locked()
            self._publish_growth_rate_status(
                GrowthRateStatus.COMPLETED,
                message_name=GROWTH_RATE_COMPLETED_MESSAGE,
            )
            return
        self.stop_image_scanning()
        with self._lock:
            processor = self.growth_rate_processor
        if processor is not None:
            final_overlay = processor.finalize()
            with self._lock:
                self.final_overlay_path = (
                    str(final_overlay) if final_overlay is not None else None
                )
        with self._lock:
            self.experiment_lifecycle_status = GrowthRateStatus.STOPPED.value
            self._persist_processing_state_locked()
        self._publish_growth_rate_status(GrowthRateStatus.STOPPED)
        with self._lock:
            self.experiment_lifecycle_status = GrowthRateStatus.COMPLETED.value
            self._persist_processing_state_locked()
        self._publish_growth_rate_status(
            GrowthRateStatus.COMPLETED,
            message_name=GROWTH_RATE_COMPLETED_MESSAGE,
        )

    def _experiment_in_progress_locked(self) -> bool:
        return self.experiment_lifecycle_status in {
            GrowthRateStatus.DISABLED.value,
            GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value,
            GrowthRateStatus.INITIALIZING.value,
            GrowthRateStatus.BASELINE_READY.value,
            GrowthRateStatus.MEASURING.value,
            GrowthRateStatus.STOPPING.value,
        }

    def _publish_growth_rate_status(
        self,
        status: GrowthRateStatus,
        *,
        frame_seq: int | None = None,
        image_name: str | None = None,
        error: str | None = None,
        run_id: str | None = None,
        message_name: str = GROWTH_RATE_STATUS_MESSAGE,
    ) -> Dict[str, Any] | None:
        with self._publication_lock:
            with self._lock:
                if status in {
                    GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE, GrowthRateStatus.INITIALIZING,
                    GrowthRateStatus.BASELINE_READY, GrowthRateStatus.MEASURING,
                } and (not self.gsensor_enabled or self._processing_transition_pending):
                    return None
                if status in {
                    GrowthRateStatus.DISABLED, GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE,
                    GrowthRateStatus.INITIALIZING, GrowthRateStatus.MEASURING,
                } and status.value != self.experiment_lifecycle_status:
                    return None
            return self._emit_growth_rate_status(
                status, frame_seq=frame_seq, image_name=image_name, error=error,
                run_id=run_id, message_name=message_name,
            )

    def _emit_growth_rate_status(
        self,
        status: GrowthRateStatus,
        *,
        frame_seq: int | None = None,
        image_name: str | None = None,
        error: str | None = None,
        run_id: str | None = None,
        message_name: str = GROWTH_RATE_STATUS_MESSAGE,
    ) -> Dict[str, Any] | None:
        status_payload = self._growth_rate_status_payload(
            status, frame_seq=frame_seq, image_name=image_name, error=error, run_id=run_id,
        )
        if status_payload is None:
            return None
        envelope = build_envelope(
            src=ROLE,
            dst="central",
            msg_type="status",
            name=message_name,
            seq=next_seq(),
            payload=status_payload.to_dict(),
        )
        with self._lock:
            self.last_status_message = envelope
            self.last_status_publish_error = None
        if self.status_publisher is not None:
            try:
                self.status_publisher.publish(route(ROLE, "central"), envelope)
            except Exception as exc:
                with self._lock:
                    self.last_status_publish_error = str(exc)
                logger.warning("Could not publish Gsensor status: %s", exc)
        self._send_controller_status(status_payload, message_name=message_name)
        return envelope

    def _growth_rate_status_payload(
        self, status: GrowthRateStatus, *, frame_seq: int | None = None,
        image_name: str | None = None, error: str | None = None, run_id: str | None = None,
    ) -> GrowthRateStatusPayload | None:
        with self._lock:
            selected_run_id = run_id or (self.current_experiment or {}).get("run_id")
            if not selected_run_id:
                return None
            return GrowthRateStatusPayload(
                run_id=str(selected_run_id), status=status, occurred_at=utc_ts(),
                frame_seq=self.measurement_step_count if frame_seq is None else frame_seq,
                image_name=image_name or self.last_detected_image, error=error,
                enabled=self.gsensor_enabled, control_revision=self.gsensor_control_revision,
                experiment_started_at=self.experiment_started_at,
                control_event_id=self.gsensor_control_event_id,
                initialization_generation=self.initialization_generation,
                alignment_revision=self.alignment_revision,
                measurement_ready=bool(
                    status == GrowthRateStatus.MEASURING and self.gsensor_enabled
                    and self.initialized and self.growth_rate_processor is not None
                    and not self._processing_transition_pending
                    and not getattr(self.growth_rate_processor, "alignment_warmup_pending", False)
                ),
            )

    def _send_controller_status(
        self, payload: GrowthRateStatusPayload, *, message_name: str = GROWTH_RATE_STATUS_MESSAGE,
    ) -> bool:
        # The same publisher/channel carries availability and samples, in that
        # order. A failed availability send must never be followed by a sample.
        envelope = build_envelope(
            src=ROLE, dst="controller", msg_type="status", name=message_name,
            seq=next_seq(), payload=payload.to_dict(),
        )
        with self._lock:
            self.last_controller_status_message = envelope
            self.last_controller_status_publish_error = None
        if self.sample_publisher is not None:
            try:
                self.sample_publisher.publish(route(ROLE, "controller"), envelope)
            except Exception as exc:
                with self._lock:
                    self.last_controller_status_publish_error = str(exc)
                logger.warning("Could not publish Gsensor availability to Controller: %s", exc)
                return False
        return True

    def _publish_transition_availability(self) -> None:
        with self._publication_lock:
            with self._lock:
                status = GrowthRateStatus(self.experiment_lifecycle_status)
                error = self.experiment_lifecycle_error if status == GrowthRateStatus.ERROR else None
            # Controller needs the gate before a worker is drained. Central
            # receives the ordinary lifecycle acknowledgment after completion.
            payload = self._growth_rate_status_payload(status, error=error)
            if payload is not None:
                self._send_controller_status(payload)

    def _sample_payload(self, result: GrowthRateFrameResult) -> GrowthRateSamplePayload:
        captured_at = result.captured_at or result.detected_at or result.processed_at
        if result.valid and result.u is not None and result.v is not None:
            values = {
                "G_u": result.u.G,
                "G_u_KF": result.u.G_KF,
                "G_v": result.v.G,
                "G_v_KF": result.v.G_KF,
            }
            error = None
        else:
            values = {"G_u": None, "G_u_KF": None, "G_v": None, "G_v_KF": None}
            error = result.error or "growth-rate frame is invalid"
        return GrowthRateSamplePayload(
            run_id=result.run_id,
            frame_seq=result.frame_seq,
            image_name=result.image_name,
            captured_at=captured_at,
            processed_at=result.processed_at,
            dt_s=result.dt_s,
            unit=result.unit,
            valid=result.valid,
            status="measured" if result.valid else "invalid",
            error=error,
            experiment_started_at=self.experiment_started_at,
            control_revision=self.gsensor_control_revision,
            initialization_generation=self.initialization_generation,
            alignment_revision=self.alignment_revision,
            **values,
        )

    def _publish_growth_rate_sample(
        self,
        result: GrowthRateFrameResult,
    ) -> Dict[str, Any] | None:
        # Serialize the send with disable: after disable takes this lock no
        # in-flight frame can emit a sample, even if its calculation completes.
        with self._publication_lock:
            with self._lock:
                if (
                    not self.gsensor_enabled
                    or self._processing_transition_pending
                    or self._measurement_stop.is_set()
                    or self.experiment_lifecycle_status != GrowthRateStatus.MEASURING.value
                    or not self.current_experiment
                    or result.run_id != self.current_experiment["run_id"]
                ):
                    return None
            availability = self._growth_rate_status_payload(
                GrowthRateStatus.MEASURING,
                frame_seq=result.frame_seq,
                image_name=result.image_name,
            )
            if availability is not None and not result.valid:
                # Close Controller's cached-G gate before sending diagnostics.
                # If the following invalid sample fails to publish, the last
                # good frame must not remain available for control/adaptation.
                availability = replace(
                    availability,
                    measurement_ready=False,
                )
            if availability is None or not self._send_controller_status(availability):
                with self._lock:
                    self.last_sample_publish_error = (
                        "Sample withheld because Controller availability could not be published."
                    )
                    self.sample_publish_failure_count += 1
                return None
            return self._emit_growth_rate_sample(result)

    def _emit_growth_rate_sample(self, result: GrowthRateFrameResult) -> Dict[str, Any]:
        payload = self._sample_payload(result)
        envelope = build_envelope(
            src=ROLE,
            dst="controller",
            msg_type="measurement",
            name=GROWTH_RATE_SAMPLE_MESSAGE,
            seq=next_seq(),
            payload=payload.to_dict(),
        )
        with self._lock:
            self.last_sample_message = envelope
            self.last_sample_publish_error = None
        if self.sample_publisher is not None:
            try:
                self.sample_publisher.publish(route(ROLE, "controller"), envelope)
            except Exception as exc:
                with self._lock:
                    self.last_sample_publish_error = str(exc)
                    self.sample_publish_failure_count += 1
                logger.warning("Could not publish growth-rate sample: %s", exc)
            else:
                with self._lock:
                    self.sample_publish_success_count += 1
        return envelope

    def _persist_growth_rate_result(self, result: GrowthRateFrameResult) -> None:
        if self.measurement_writer is None:
            return
        u = result.u
        v = result.v
        sample = self._sample_payload(result)
        record = GsensorMeasurementRecord(
            run_id=sample.run_id,
            frame_seq=sample.frame_seq,
            image_name=sample.image_name,
            captured_at=sample.captured_at,
            processed_at=sample.processed_at,
            dt_s=sample.dt_s,
            valid=sample.valid,
            G_u=sample.G_u,
            G_u_KF=sample.G_u_KF,
            G_v=sample.G_v,
            G_v_KF=sample.G_v_KF,
            error=sample.error,
            unit=sample.unit,
            processing_duration_ms=result.processing_duration_ms,
            u_distance_px=u.distance_px if u is not None else None,
            u_distance_m=u.distance_m if u is not None else None,
            v_distance_px=v.distance_px if v is not None else None,
            v_distance_m=v.distance_m if v is not None else None,
        )
        try:
            write_gsensor_measurement(self.measurement_writer, record)
        except Exception as exc:
            with self._lock:
                self.last_influx_error = str(exc)
                self.influx_write_failure_count += 1
            logger.warning("Could not persist growth-rate measurement: %s", exc)
        else:
            with self._lock:
                self.last_influx_write_at = utc_ts()
                self.last_influx_error = None
                self.influx_write_success_count += 1

    @staticmethod
    def _validate_central_lifecycle_message(message: Dict[str, Any]) -> None:
        if message.get("src") != "central" or message.get("dst") != ROLE:
            raise ValueError("Experiment lifecycle commands must be sent from central to gsensor.")
        if message.get("msg_type") != "command":
            raise ValueError("Experiment lifecycle messages must use msg_type='command'.")

    def start_image_scanning(self) -> None:
        with self._control_lock:
            self._start_image_scanning()

    def _start_image_scanning(self) -> None:
        previous_thread: threading.Thread | None
        with self._lock:
            if not self.gsensor_enabled or self.experiment_lifecycle_status not in {
                GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value, GrowthRateStatus.INITIALIZING.value,
                GrowthRateStatus.BASELINE_READY.value, GrowthRateStatus.MEASURING.value,
            }:
                raise ValueError("GSensor is disabled; an explicit enable command is required.")
            if self._measurement_thread and self._measurement_thread.is_alive() and not self._measurement_stop.is_set():
                self.active = self.experiment_lifecycle_status != GrowthRateStatus.INITIALIZING.value
                return
            previous_thread = self._measurement_thread

        if (
            previous_thread
            and previous_thread.is_alive()
            and previous_thread is not threading.current_thread()
        ):
            previous_thread.join(timeout=2)
            if previous_thread.is_alive():
                raise TimeoutError("The previous Gsensor worker has not stopped.")

        with self._lock:
            self.experiments.require_current()
            self._measurement_stop.clear()
            self.active = self.experiment_lifecycle_status != GrowthRateStatus.INITIALIZING.value
            self.image_scan_status = "waiting_for_image"
            self.image_scan_error = None
            self._measurement_thread = threading.Thread(
                target=self._run_image_polling_loop,
                name="gsensor-image-poller",
                daemon=True,
            )
            self._measurement_thread.start()

    def stop_image_scanning(self) -> None:
        thread: threading.Thread | None
        with self._lock:
            self.active = False
            self._measurement_stop.set()
            thread = self._measurement_thread

        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=30)
            if thread.is_alive():
                raise TimeoutError(
                    "Gsensor image processing did not stop within 30 seconds."
                )

        with self._lock:
            self.image_scan_status = "stopped"

    def _run_image_polling_loop(self) -> None:
        try:
            while not self._measurement_stop.is_set():
                try:
                    self._scan_current_image_directory()
                except Exception as exc:
                    logger.exception("Unexpected Gsensor image polling failure.")
                    with self._lock:
                        self.last_image_scan_at = utc_ts()
                        self.image_scan_error = str(exc)
                        self.image_scan_status = (
                            "stopped" if self._measurement_stop.is_set() else "error"
                        )
                self._measurement_stop.wait(self.image_poll_interval_s)
        finally:
            with self._lock:
                if self._measurement_stop.is_set():
                    self.active = False
                    self.image_scan_status = "stopped"

    def _scan_current_image_directory(self) -> None:
        with self._image_scan_lock:
            self._scan_image_directory_locked()

    def _scan_image_directory_locked(self) -> None:
        with self._lock:
            if not self.gsensor_enabled or self._measurement_stop.is_set() or self._processing_transition_pending:
                return
            selection = dict(self.experiments.require_current())
            processed_before = self.processed_image_files | self.ignored_image_files
            observations_before = dict(self.image_observations)
            lifecycle_status = self.experiment_lifecycle_status
            processor = self.growth_rate_processor
            discovery_revision = self.image_discovery_revision

        try:
            result = scan_new_images(
                selection["container_image_path"],
                processed_before,
                observations=observations_before,
                minimum_stable_scans=2,
                image_probe=self.image_probe,
            )
        except Exception as exc:
            with self._lock:
                if not self.gsensor_enabled or discovery_revision != self.image_discovery_revision:
                    return
                self.last_image_scan_at = utc_ts()
                self.image_scan_status = "error"
                self.image_scan_error = str(exc)
                self.image_observations.clear()
            logger.warning("Gsensor image scan failed: %s", exc)
            return

        with self._lock:
            current_run_id = (
                self.current_experiment.get("run_id")
                if self.current_experiment is not None
                else None
            )
            if (
                not self.gsensor_enabled
                or self._processing_transition_pending
                or self._measurement_stop.is_set()
                or discovery_revision != self.image_discovery_revision
                or current_run_id != selection["run_id"]
                or lifecycle_status != self.experiment_lifecycle_status
            ):
                return
            self.image_observations = {
                item.image_name: item for item in result.observations
            }
            self.pending_image_count = result.pending_image_count
            self.last_image_scan_at = result.scanned_at
            self.image_scan_error = result.last_error
            for field in ("latest_discovered_image", "latest_ready_image"):
                record = getattr(self, field)
                if record and record["identity_key"] not in result.file_identities:
                    setattr(self, field, None)
            committed = self.sequence_watermark_record
            if (
                committed and committed.get("identity_key") in result.file_identities
                and committed.get("identity_key") not in self.ignored_image_files
            ):
                # A committed, decoded revision is still usable while its exact
                # signature remains present. Pending newer revisions do not replace it.
                if self.latest_ready_image is None:
                    self.latest_ready_image = dict(committed)
                if self.latest_discovered_image is None:
                    self.latest_discovered_image = dict(committed)
            self._record_latest_discovered_locked(result.latest_discovered)

            accepted: list[DetectedImage] = []
            late: list[DetectedImage] = []
            for detection in result.detections:
                if (
                    detection.sequence_number is not None
                    and self.sequence_watermark is not None
                    and detection.sequence_number <= self.sequence_watermark
                ):
                    late.append(detection)
                else:
                    accepted.append(detection)

            for detection in late:
                record = self._detected_image_record(
                    detection,
                    frame_seq=None,
                    disposition="late",
                )
                self.processed_image_files.add(detection.identity_key)
                self.processed_image_records[detection.identity_key] = record
                self.late_image_count += 1
                self.last_late_image = record
                if detection.sequence_number in {0, 1} and self.sequence_watermark > 2:
                    self.sequence_reset_candidate = record

            if accepted:
                self._record_latest_ready_locked(
                    self._latest_initialization_detection(tuple(accepted))
                )
            handled_before = set(self.processed_image_files)

        if (
            lifecycle_status == GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
            and accepted
        ):
            latest = self._latest_initialization_detection(tuple(accepted))
            image_path = Path(selection["container_image_path"]) / latest.image_name
            try:
                # Keep marking and subsequent alignment on the exact admitted pixels,
                # even if the camera later overwrites the same filename.
                content = image_path.read_bytes()
                stat = image_path.stat()
                if image_identity_key(
                    latest.image_name, modified_time_ns=stat.st_mtime_ns, file_size=stat.st_size,
                ) != latest.identity_key:
                    with self._lock:
                        self.image_observations.pop(latest.image_name, None)
                        self.latest_ready_image = None
                    return
                snapshot_path = image_path.parent.parent / "gsensor_reference" / uuid4().hex / latest.image_name
                snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                snapshot_path.write_bytes(content)
                initialization = GsensorInitializationManager()
                initialization_payload = initialization.start_image(
                    snapshot_path, source_folder=image_path.parent,
                )
            except OSError as exc:
                with self._lock:
                    if not self.gsensor_enabled or discovery_revision != self.image_discovery_revision:
                        return
                    self.image_observations.pop(latest.image_name, None)
                    self.latest_ready_image = None
                    self.image_scan_error = str(exc)
                    self.image_scan_status = "waiting_for_image"
                return
            except Exception as exc:
                with self._lock:
                    if not self.gsensor_enabled or discovery_revision != self.image_discovery_revision:
                        return
                    self.active = False
                    self.gsensor_enabled = False
                    self._measurement_stop.set()
                    self.last_image_scan_at = result.scanned_at
                    self.image_scan_status = "error"
                    self.image_scan_error = str(exc)
                    self.experiment_lifecycle_status = GrowthRateStatus.ERROR.value
                    self.experiment_lifecycle_error = str(exc)
                self._publish_growth_rate_status(
                    GrowthRateStatus.ERROR,
                    image_name=latest.image_name,
                    error=str(exc),
                )
                return

            with self._lock:
                if (
                    not self.gsensor_enabled
                    or self._processing_transition_pending
                    or self.experiment_lifecycle_status
                    != GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
                    or discovery_revision != self.image_discovery_revision
                ):
                    return
                self.initialization = initialization
                for detection in accepted:
                    is_baseline = detection.identity_key == latest.identity_key
                    self.processed_image_files.add(detection.identity_key)
                    self.processed_image_records[detection.identity_key] = (
                        self._detected_image_record(
                            detection,
                            frame_seq=0 if is_baseline else None,
                            disposition="baseline" if is_baseline else "backlog",
                        )
                    )
                self._advance_sequence_watermark_locked(
                    latest,
                    frame_seq=0,
                    record_gap=False,
                )
                self.last_detected_image = latest.image_name
                self.file_modified_at = latest.file_modified_at
                self.detected_at = latest.detected_at
                self.initialized = False
                self.initialization_status = str(
                    initialization_payload.get("status") or "awaiting_is_full"
                )
                self.experiment_lifecycle_status = GrowthRateStatus.INITIALIZING.value
                self.experiment_lifecycle_error = None
                self.image_scan_status = "monitoring_during_initialization"
                self.active = False
                self._persist_processing_state_locked()
            self._publish_growth_rate_status(
                GrowthRateStatus.INITIALIZING,
                frame_seq=0,
                image_name=latest.image_name,
            )
            return

        if lifecycle_status == GrowthRateStatus.MEASURING.value:
            self._process_detected_images(
                selection,
                tuple(accepted),
                processor,
                processed_before=handled_before,
                scan_pending_count=result.pending_image_count,
                scanned_at=result.scanned_at,
                scan_error=result.last_error,
            )
            return

        with self._lock:
            if not self.gsensor_enabled or discovery_revision != self.image_discovery_revision:
                return
            if lifecycle_status == GrowthRateStatus.INITIALIZING.value:
                self.image_scan_status = "monitoring_during_initialization"
                self._persist_processing_state_locked()
                return
            self.image_scan_status = (
                "running" if accepted else "waiting_for_image"
            )
            for detection in accepted:
                self.processed_image_files.add(detection.identity_key)
                self.processed_image_records[detection.identity_key] = self._detected_image_record(
                    detection, frame_seq=None, disposition="observed"
                )
                self._advance_sequence_watermark_locked(detection, frame_seq=0)
                self.last_detected_image = detection.image_name
                self.file_modified_at = detection.file_modified_at
                self.detected_at = detection.detected_at
                self.last_measurement_step_at = detection.detected_at
                self.measurement_step_count += 1
            self._persist_processing_state_locked()

    @staticmethod
    def _latest_initialization_detection(
        detections: tuple[DetectedImage, ...],
    ) -> DetectedImage:
        numbered = [item for item in detections if item.sequence_number is not None]
        if numbered:
            return max(
                numbered,
                key=lambda item: (
                    int(item.sequence_number or 0),
                    item.modified_time_ns,
                    item.image_name.casefold(),
                ),
            )
        return detections[-1]

    def _record_latest_discovered_locked(
        self,
        observation: ImageObservation | None,
    ) -> None:
        if observation is None:
            return
        if (
            observation.sequence_number is not None
            and self.sequence_watermark is not None
            and observation.sequence_number <= self.sequence_watermark
        ):
            return
        candidate = self._observation_record(observation)
        if self._source_record_is_newer(candidate, self.latest_discovered_image):
            self.latest_discovered_image = candidate

    def _record_latest_ready_locked(self, detection: DetectedImage) -> None:
        candidate = self._detected_image_record(
            detection,
            frame_seq=None,
            disposition="ready",
        )
        if self._source_record_is_newer(candidate, self.latest_ready_image):
            self.latest_ready_image = candidate

    @staticmethod
    def _source_record_is_newer(
        candidate: Dict[str, Any],
        current: Dict[str, Any] | None,
    ) -> bool:
        if current is None:
            return True
        candidate_sequence = candidate.get("source_sequence")
        current_sequence = current.get("source_sequence")
        if candidate_sequence is not None and current_sequence is not None:
            return int(candidate_sequence) >= int(current_sequence)
        if candidate_sequence is not None:
            return True
        if current_sequence is not None:
            return False
        return int(candidate.get("modified_time_ns") or 0) > int(
            current.get("modified_time_ns") or 0
        )

    def _advance_sequence_watermark_locked(
        self,
        detection: DetectedImage,
        *,
        frame_seq: int,
        record_gap: bool = True,
    ) -> None:
        sequence = detection.sequence_number
        if sequence is None:
            return
        if (
            record_gap
            and self.sequence_watermark is not None
            and sequence > self.sequence_watermark + 1
        ):
            self.missing_sequence_ranges.append(
                {
                    "epoch": self.sequence_epoch,
                    "start": self.sequence_watermark + 1,
                    "end": sequence - 1,
                    "recorded_at": detection.detected_at,
                }
            )
        self.sequence_watermark = sequence
        self.sequence_watermark_record = {
            **self._detected_image_record(
                detection,
                frame_seq=frame_seq,
                disposition="committed",
            ),
            "sequence_epoch": self.sequence_epoch,
            "committed_at": utc_ts(),
        }

    def _process_detected_images(
        self,
        selection: Dict[str, Any],
        detections: tuple[DetectedImage, ...],
        processor: GrowthRateProcessor | None,
        *,
        processed_before: set[str],
        scan_pending_count: int,
        scanned_at: str,
        scan_error: str | None,
    ) -> None:
        with self._lock:
            if not self.gsensor_enabled or self._measurement_stop.is_set() or self._processing_transition_pending:
                return
        processed = set(processed_before)
        remaining = len(detections)

        if processor is None:
            error = "Growth-rate processor is not initialized."
            with self._lock:
                if not self.gsensor_enabled:
                    return
                self.gsensor_enabled = False
                self.image_scan_status = "error"
                self.image_scan_error = error
                self.last_processing_error = error
                self.experiment_lifecycle_status = GrowthRateStatus.ERROR.value
                self.experiment_lifecycle_error = error
                self.active = False
                self._measurement_stop.set()
            self._publish_growth_rate_status(GrowthRateStatus.ERROR, error=error)
            return

        for detection in detections:
            if self._measurement_stop.is_set():
                break
            with self._lock:
                if not self.gsensor_enabled or self._processing_transition_pending or self.experiment_lifecycle_status != GrowthRateStatus.MEASURING.value:
                    break
                if (
                    detection.sequence_number is not None
                    and self.sequence_watermark is not None
                    and detection.sequence_number <= self.sequence_watermark
                ):
                    # Two filenames/revisions can carry the same sequence in one scan.
                    record = self._detected_image_record(detection, frame_seq=None, disposition="late")
                    self.processed_image_records[detection.identity_key] = record
                    self.processed_image_files.add(detection.identity_key)
                    processed.add(detection.identity_key)
                    self.late_image_count += 1
                    self.last_late_image = record
                    remaining -= 1
                    continue
            image_path = Path(selection["container_image_path"]) / detection.image_name
            stat = image_path.stat()
            if image_identity_key(
                detection.image_name, modified_time_ns=stat.st_mtime_ns, file_size=stat.st_size,
            ) != detection.identity_key:
                # A revision replaced after discovery needs another stable scan.
                continue
            frame_result = processor.process(
                image_path,
                captured_at=detection.file_modified_at,
                detected_at=detection.detected_at,
            )
            processed.add(detection.identity_key)
            remaining -= 1
            self._record_processed_frame(detection, frame_result)
            self._publish_growth_rate_sample(frame_result)
            with self._publication_lock:
                with self._lock:
                    should_persist = self.gsensor_enabled and not self._measurement_stop.is_set() and not self._processing_transition_pending
                if should_persist:
                    self._persist_growth_rate_result(frame_result)
            self._publish_growth_rate_status(
                GrowthRateStatus.MEASURING,
                frame_seq=frame_result.frame_seq,
                image_name=frame_result.image_name,
            )

        with self._lock:
            self.processed_image_files.update(processed)
            self.pending_image_count = scan_pending_count + remaining
            self.last_image_scan_at = scanned_at
            self.image_scan_error = scan_error
            if not self.gsensor_enabled or self._measurement_stop.is_set():
                self.image_scan_status = "stopped"
            elif detections and len(detections) != remaining:
                self.image_scan_status = "running"
            else:
                self.image_scan_status = "waiting_for_image"
            self._persist_processing_state_locked()

    def _record_processed_frame(
        self,
        detection: DetectedImage,
        result: GrowthRateFrameResult,
    ) -> None:
        payload = result.to_dict()
        with self._lock:
            self.processed_image_files.add(detection.identity_key)
            self.processed_image_records[detection.identity_key] = (
                self._detected_image_record(
                    detection,
                    frame_seq=result.frame_seq,
                    disposition="processed",
                )
            )
            self._advance_sequence_watermark_locked(
                detection,
                frame_seq=result.frame_seq,
            )
            self.last_detected_image = detection.image_name
            self.file_modified_at = detection.file_modified_at
            self.detected_at = detection.detected_at
            self.last_measurement_step_at = result.processed_at
            self.measurement_step_count = result.frame_seq
            self.measurement_valid_frame_count += int(result.valid)
            self.measurement_invalid_frame_count += int(not result.valid)
            self.last_growth_rate_result = payload
            self.last_growth_rate_result["initialization_generation"] = self.initialization_generation
            self.last_growth_rate_result["alignment_revision"] = self.alignment_revision
            self.processed_image_records[detection.identity_key]["initialization_generation"] = self.initialization_generation
            self.processed_image_records[detection.identity_key]["alignment_revision"] = self.alignment_revision
            self.last_processing_error = result.error
            self.latest_overlay_path = result.overlay_path
            self._persist_processing_state_locked()
        logger.warning(
            "Gsensor online frame done: run_id=%s frame_seq=%s image=%s valid=%s error=%s",
            result.run_id,
            result.frame_seq,
            result.image_name,
            result.valid,
            result.error,
        )

    @staticmethod
    def _detected_image_record(
        detection: DetectedImage,
        *,
        frame_seq: int | None,
        disposition: str = "processed",
    ) -> Dict[str, Any]:
        return {
            "identity_key": detection.identity_key,
            "image_name": detection.image_name,
            "source_sequence": detection.sequence_number,
            "modified_time_ns": detection.modified_time_ns,
            "file_size": detection.file_size,
            "file_modified_at": detection.file_modified_at,
            "detected_at": detection.detected_at,
            "frame_seq": int(frame_seq) if frame_seq is not None else None,
            "disposition": disposition,
        }

    @staticmethod
    def _observation_record(observation: ImageObservation) -> Dict[str, Any]:
        return {
            "identity_key": observation.identity_key,
            "image_name": observation.image_name,
            "source_sequence": observation.sequence_number,
            "modified_time_ns": observation.modified_time_ns,
            "file_size": observation.file_size,
            "file_modified_at": observation.file_modified_at,
            "stable_scan_count": observation.stable_scan_count,
        }

    def _measurement_running(self) -> bool:
        return self._measurement_thread is not None and self._measurement_thread.is_alive()

    def _active_params_path(self) -> Path:
        if self.params_path.exists():
            return self.params_path
        return self.default_params_path

    def _load_persisted_params(self) -> tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
        with self._lock:
            if self._experiment_in_progress_locked():
                return load_params(str(self._active_params_path()))
        # Central can have an immutable STARTING snapshot before GSensor has
        # received experiment.start. Consult the shared manifest as well.
        central = CentralExperimentManager(self.experiment_root_path)
        run_id = central.current_run_id()
        if run_id is not None and central.registry.get(run_id).status not in {
            ExperimentStatus.CREATED, ExperimentStatus.COMPLETED, ExperimentStatus.ERROR,
        }:
            return load_params(str(self._active_params_path()))
        return load_runtime_params(str(self.params_path), str(self.default_params_path))

    def load_param_meta(self) -> Dict[str, Dict[str, Any]]:
        return load_param_meta(str(self.param_meta_path))

    def current_params(self) -> Dict[str, Any]:
        shared, gsensor, _controller, _version = self._load_persisted_params()
        with self._lock:
            return {**shared, **gsensor, **self.params}

    def _alignment_configuration_locked(self) -> Dict[str, Any]:
        params = self.experiment_params or self.current_params()
        configured = parse_alignment_method(params.get("alignment_method", "none")).value
        confirmed = self.confirmed_alignment_method
        # Older in-memory integrations may construct an initialized processor
        # without the new confirmation attributes.
        if confirmed is None and self.initialized and self.growth_rate_processor is not None:
            confirmed = parse_alignment_method(
                getattr(self.growth_rate_processor, "alignment_method", configured)
            ).value
        return {
            "method": confirmed or configured,
            "configured_method": configured,
            "confirmed": confirmed is not None,
            "confirmed_at": self.alignment_confirmed_at or (self.initialized_at if confirmed else None),
            "revision": self.alignment_revision,
            "effective_after_frame_seq": self.alignment_effective_after_frame_seq,
            "in_progress": self._alignment_change_pending,
            "last_error": self.alignment_change_error,
            "warmup_pending": bool(getattr(self.growth_rate_processor, "alignment_warmup_pending", False)),
            "can_select": (
                self.gsensor_enabled
                and not self._processing_transition_pending
                and ((self.experiment_lifecycle_status == GrowthRateStatus.INITIALIZING.value
                      and not self.initialized) or self._can_switch_alignment_locked())
            ),
            "can_switch": self._can_switch_alignment_locked(),
        }

    @property
    def _processing_transition_pending(self) -> bool:
        return self._reinitialization_pending or self._alignment_change_pending

    def _can_switch_alignment_locked(self) -> bool:
        return bool(
            self.gsensor_enabled and self.initialized and self.growth_rate_processor is not None
            and not self._processing_transition_pending
            and self.experiment_lifecycle_status == GrowthRateStatus.MEASURING.value
        )

    def select_alignment_method(
        self, run_id: str, session_id: str, control_revision: int,
        alignment_revision: int, alignment_method: str,
    ) -> Dict[str, Any]:
        """Switch the live method at a frame boundary, preserving manual marks."""
        selected = parse_alignment_method(alignment_method).value
        with self._control_lock:
            with self._publication_lock, self._lock:
                selection = self.experiments.require_current()
                if run_id != selection["run_id"]:
                    raise ValueError("The experiment changed. Refresh before changing alignment.")
                if session_id != self.initialization.active_session_id:
                    raise ValueError("The marking session changed. Refresh before changing alignment.")
                if type(control_revision) is not int or control_revision != self.gsensor_control_revision:
                    raise ValueError("GSensor activation changed. Refresh before changing alignment.")
                if type(alignment_revision) is not int or alignment_revision != self.alignment_revision:
                    raise ValueError("The alignment selection changed. Refresh before changing it again.")
                if not self._can_switch_alignment_locked():
                    raise ValueError("Enable GSensor and confirm image marking before changing live alignment.")
                if self.experiments.registry.get(run_id).status in TERMINAL_EXPERIMENT_STATUSES:
                    raise ValueError("A finished experiment cannot change alignment.")
                capability = next((item for item in alignment_capabilities() if item.method == selected), None)
                if capability is None or not capability.available:
                    reason = capability.reason if capability is not None else "not available"
                    raise ValueError(f"Alignment method {selected!r} is unavailable: {reason}")
                if selected == self._alignment_configuration_locked()["method"]:
                    return self.status()
                # Close publication before draining the scanner. Never wait for
                # the scanner while holding the lock it needs to finish a frame.
                self._alignment_change_pending = True
                self.alignment_change_error = None
                self.image_discovery_revision += 1
            try:
                self._publish_transition_availability()
                with self._image_scan_lock:
                    self._replace_alignment_processor(selection, selected)
            except Exception as exc:
                with self._lock:
                    self.alignment_change_error = str(exc)
                raise
            finally:
                with self._lock:
                    self._alignment_change_pending = False
                self._publish_transition_availability()
            return self.status()

    def _replace_alignment_processor(self, selection: Dict[str, Any], selected: str) -> None:
        session_id = self.initialization.active_session_id
        payload = self.initialization.payload(session_id)
        uv_structs, kernel = initialize_DSCGR(self.initialization, session_id=session_id)
        revision = self.alignment_revision + 1
        output_directory = self._measurement_output_directory_locked(alignment_revision=revision)
        processor = self.growth_rate_processor_factory(
            run_id=selection["run_id"],
            params=copy.deepcopy(self.experiment_params or self.current_params()),
            uv_struct_list=uv_structs, kernel=kernel,
            latest_overlay_path=output_directory / LATEST_OVERLAY_FILENAME,
            final_overlay_path=output_directory / FINAL_OVERLAY_FILENAME,
            debug_directory=(output_directory / "hough_debug" if self.hough_debug_enabled else None),
            initial_image_path=payload["selected_image"], alignment_method=selected,
        )
        processor.begin_alignment_segment(
            frame_seq=self.measurement_step_count,
            valid_frame_count=self.measurement_valid_frame_count,
            invalid_frame_count=self.measurement_invalid_frame_count,
        )
        processor.prime_alignment_from_baseline()
        candidate_sidecar = output_directory / "alignment_state.npz"
        sidecar_existed = candidate_sidecar.exists()
        # Archive the already committed record. Exporting the old processor
        # again here could rewrite its sidecar before this transaction commits.
        checkpoint = self.experiments.load_processing_state(selection["run_id"])
        if checkpoint is None:
            raise ValueError("The current processing checkpoint is missing; alignment was not changed.")
        archive = self.experiments.save_reinitialization_snapshot(
            selection["run_id"], checkpoint, kind="processing",
        )
        changed_at = utc_ts()
        run_directory = Path(selection["container_image_path"]).parent
        with self._lock:
            updates = {
                "growth_rate_processor": processor, "uv_struct_list": processor.uv_structs,
                "kernel": kernel, "confirmed_alignment_method": selected,
                "alignment_confirmed_at": changed_at, "alignment_revision": revision,
                "alignment_effective_after_frame_seq": self.measurement_step_count,
                "last_processing_error": None, "final_overlay_path": None,
                "alignment_changes": [*self.alignment_changes, {
                    "revision": revision, "session_id": session_id,
                    "initialization_generation": self.initialization_generation,
                    "from_method": self._alignment_configuration_locked()["method"],
                    "method": selected, "changed_at": changed_at,
                    "effective_after_frame_seq": self.measurement_step_count,
                    "previous_state_file": archive.relative_to(run_directory).as_posix(),
                }],
            }
            previous = {key: getattr(self, key) for key in updates}
            try:
                for key, value in updates.items():
                    setattr(self, key, value)
                self._persist_processing_state_locked()
            except Exception:
                for key, value in previous.items():
                    setattr(self, key, value)
                archive.unlink(missing_ok=True)
                if not sidecar_existed:
                    candidate_sidecar.unlink(missing_ok=True)
                raise


    def _restore_alignment_configuration_locked(
        self, configuration: Any, *, has_processor: bool
    ) -> None:
        self.confirmed_alignment_method = None
        self.alignment_confirmed_at = None
        self.alignment_revision = 0
        self.alignment_effective_after_frame_seq = 0
        if configuration is None:
            # Older runs used the Start-time method and have no separate choice.
            if has_processor:
                self.confirmed_alignment_method = parse_alignment_method(
                    (self.experiment_params or {}).get("alignment_method", "none")
                ).value
                self.alignment_confirmed_at = self.initialized_at
            return
        if not isinstance(configuration, dict) or not isinstance(
            configuration.get("confirmed"), bool
        ):
            raise ValueError("Persisted alignment configuration is invalid.")
        method = parse_alignment_method(configuration.get("method")).value
        revision = configuration.get("revision", 0)
        effective_after = configuration.get("effective_after_frame_seq", 0)
        if type(revision) is not int or revision < 0 or type(effective_after) is not int or effective_after < 0:
            raise ValueError("Persisted alignment revision or frame boundary is invalid.")
        self.alignment_revision = revision
        self.alignment_effective_after_frame_seq = effective_after
        if configuration["confirmed"]:
            confirmed_at = configuration.get("confirmed_at")
            if confirmed_at is not None and not isinstance(confirmed_at, str):
                raise ValueError("Persisted alignment confirmation time is invalid.")
            self.confirmed_alignment_method = method
            self.alignment_confirmed_at = confirmed_at
        elif has_processor:
            raise ValueError("Processor recovery requires a confirmed alignment method.")

    def _experiment_parameters_locked(self) -> Dict[str, Any] | None:
        if self.current_experiment is None or self.experiment_params is None:
            return None
        run_id = self.current_experiment["run_id"]
        configuration = self._alignment_configuration_locked()
        params = copy.deepcopy(self.experiment_params)
        params["alignment_method"] = configuration["method"]
        return {
            "run_id": run_id,
            "label": self.experiments.registry.get(run_id).label,
            "parameter_version": self.experiment_parameter_version,
            "params": params,
            "alignment_configuration": configuration,
        }

    def params_payload(self) -> Dict[str, Any]:
        shared, gsensor, _controller, persisted_version = self._load_persisted_params()
        default_shared, default_gsensor, _default_controller, default_version = load_params(
            str(self.default_params_path)
        )
        with self._lock:
            locked = self._experiment_in_progress_locked()
            active_params = dict(self.experiment_params or self.params) if locked else None
            active_version = self.experiment_parameter_version if locked else None
        params = active_params or {**shared, **gsensor}
        version = int(active_version) if active_version is not None else persisted_version
        return {
            "version": version,
            "params": params,
            "defaults": {
                "version": default_version,
                "params": {**default_shared, **default_gsensor},
            },
            "meta": self.load_param_meta(),
            "source_file": str(self._active_params_path()),
            "runtime_file": str(self.params_path),
            "status": self._ui_parameter_status(
                params=params,
                defaults={**default_shared, **default_gsensor},
                version=version,
            ),
        }

    def ui_config(self) -> Dict[str, str | bool]:
        return ui_mode_payload(self.ui_mode)

    def _ui_parameter_status(
        self,
        *,
        params: Dict[str, Any],
        defaults: Dict[str, Any],
        version: int,
    ) -> Dict[str, Any]:
        using_defaults = params == defaults
        saved_at = None
        if self.params_path.is_file():
            modified_at = datetime.fromtimestamp(
                self.params_path.stat().st_mtime,
                tz=timezone.utc,
            )
            saved_at = modified_at.isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._lock:
            locked = self._experiment_in_progress_locked()
            run_id = self.current_experiment.get("run_id") if self.current_experiment else None
            applied = (
                run_id is not None
                and self.experiment_parameter_version is not None
                and int(self.experiment_parameter_version) == int(version)
            )
        if applied:
            kind = "applied"
            message = f"Applied to {run_id} · version {version}"
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
            "locked": locked,
            "applied_run_id": run_id if applied else None,
        }

    def _measurement_output_directory_locked(self, *, alignment_revision: int | None = None) -> Path:
        selection = self.experiments.require_current()
        directory = Path(selection["container_image_path"]).parent
        if self.initialization_generation > 0:
            directory = directory / "gsensor_segments" / str(self.initialization_generation)
        revision = self.alignment_revision if alignment_revision is None else alignment_revision
        if revision > 0:
            directory = directory / "gsensor_alignments" / str(revision)
        return directory

    def _can_restart_initialization_locked(self) -> bool:
        return bool(
            self.gsensor_enabled and not self._processing_transition_pending
            and self.latest_ready_image is not None
            and self.experiment_lifecycle_status in {
                GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value,
                GrowthRateStatus.INITIALIZING.value,
                GrowthRateStatus.MEASURING.value,
            }
        )

    def restart_initialization(
        self, run_id: str, session_id: str | None, control_revision: int,
    ) -> Dict[str, Any]:
        """Replace marking with a frozen latest-ready image in the current run."""
        with self._control_lock:
            with self._publication_lock, self._lock:
                selection = self.experiments.require_current()
                if selection["run_id"] != run_id:
                    raise ValueError("The experiment changed. Refresh before re-marking.")
                if session_id != self.initialization.active_session_id:
                    raise ValueError("The initialization session changed. Refresh before re-marking.")
                if type(control_revision) is not int or control_revision != self.gsensor_control_revision:
                    raise ValueError("GSensor activation changed. Refresh before re-marking.")
                if not self._can_restart_initialization_locked():
                    raise ValueError("Enable GSensor and wait for a ready image before re-marking.")
                if self.experiments.registry.get(run_id).status in TERMINAL_EXPERIMENT_STATUSES:
                    raise ValueError("A finished experiment cannot be re-marked.")
                # Do not hold the publication lock while draining the scanner:
                # its in-flight frame needs that lock to observe this closed gate.
                self._reinitialization_pending = True
                self.reinitialization_error = None
                self.image_discovery_revision += 1
            try:
                self._publish_transition_availability()
                with self._image_scan_lock:
                    self._restart_initialization_locked(selection)
            except Exception as exc:
                with self._lock:
                    self.reinitialization_error = str(exc)
                raise
            finally:
                with self._lock:
                    self._reinitialization_pending = False
                self._publish_transition_availability()
            self._publish_growth_rate_status(
                GrowthRateStatus.INITIALIZING,
                frame_seq=self.measurement_step_count,
                image_name=self.last_detected_image,
            )
            self.start_image_scanning()
            return self.status()

    def _restart_initialization_locked(self, selection: Dict[str, Any]) -> None:
        # Called with the scanner drained and control changes serialized. A new
        # observation of a previously ready/committed revision can revalidate it
        # even though regular polling no longer includes processed revisions.
        with self._lock:
            observations = dict(self.image_observations)
            for record in (self.latest_ready_image, self.sequence_watermark_record):
                if record is None:
                    continue
                observations[record["image_name"]] = ImageObservation(
                    image_name=record["image_name"], identity_key=record["identity_key"],
                    sequence_number=record.get("source_sequence"),
                    modified_time_ns=record["modified_time_ns"], file_size=record["file_size"],
                    file_modified_at=record["file_modified_at"], stable_scan_count=1,
                )
            ignored = set(self.ignored_image_files)
            watermark = self.sequence_watermark
        scan = scan_new_images(
            selection["container_image_path"], ignored,
            observations=observations, minimum_stable_scans=2, image_probe=self.image_probe,
        )
        ready = tuple(item for item in scan.detections if (
            item.sequence_number is None or watermark is None or item.sequence_number >= watermark
        ))
        if not ready:
            raise ValueError("No current ready image is available. Wait for the next stable image.")
        latest = self._latest_initialization_detection(ready)
        source = Path(selection["container_image_path"]) / latest.image_name
        before = source.stat()
        if image_identity_key(source.name, modified_time_ns=before.st_mtime_ns, file_size=before.st_size) != latest.identity_key:
            raise ValueError("The latest image changed while being selected. Retry after it is stable.")
        content = source.read_bytes()
        after = source.stat()
        if (image_identity_key(source.name, modified_time_ns=after.st_mtime_ns, file_size=after.st_size)
                != latest.identity_key or len(content) != latest.file_size):
            raise ValueError("The latest image is still being transferred. Retry after it is stable.")
        snapshot = source.parent.parent / "gsensor_reference" / uuid4().hex / source.name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        archive_path: Path | None = None
        try:
            snapshot.write_bytes(content)
            verify_image_readable(snapshot)
            initialization = GsensorInitializationManager()
            payload = initialization.start_image(snapshot, source_folder=source.parent)
            with self._lock:
                checkpoint = self._processing_state_document_locked()
                archive_path = self.experiments.save_reinitialization_snapshot(
                    selection["run_id"], checkpoint, kind="processing",
                )
                run_directory = source.parent.parent
                generation = self.initialization_generation + 1
                updates = {
                    "initialization": initialization,
                    "initialization_generation": generation,
                    "reinitialization_history": [*self.reinitialization_history, {
                        "generation": self.initialization_generation,
                        "session_id": self.initialization.active_session_id,
                        "state_file": archive_path.relative_to(run_directory).as_posix(),
                        "replaced_at": utc_ts(),
                    }],
                    "initialized": False, "initialized_at": None,
                    "initialization_status": payload["status"],
                    "growth_rate_processor": None, "uv_struct_list": None, "kernel": None,
                    "baseline": None, "last_growth_rate_result": None, "last_processing_error": None,
                    "latest_overlay_path": None, "final_overlay_path": None,
                    "confirmed_alignment_method": None, "alignment_confirmed_at": None,
                    "experiment_lifecycle_status": GrowthRateStatus.INITIALIZING.value,
                    "experiment_lifecycle_error": None,
                    "gsensor_resume_status": GrowthRateStatus.INITIALIZING.value,
                    "active": False, "image_scan_status": "monitoring_during_initialization",
                    "image_scan_error": scan.last_error,
                    "last_detected_image": latest.image_name,
                    "file_modified_at": latest.file_modified_at, "detected_at": latest.detected_at,
                    "latest_ready_image": self._detected_image_record(latest, frame_seq=None, disposition="ready"),
                    "processed_image_files": set(self.processed_image_files),
                    "processed_image_records": dict(self.processed_image_records),
                    "missing_sequence_ranges": list(self.missing_sequence_ranges),
                    "image_observations": {item.image_name: item for item in scan.observations},
                    "pending_image_count": scan.pending_image_count,
                    "last_image_scan_at": scan.scanned_at,
                    "sequence_watermark": self.sequence_watermark,
                    "sequence_watermark_record": self.sequence_watermark_record,
                }
                previous = {key: getattr(self, key) for key in updates}
                try:
                    for key, value in updates.items():
                        setattr(self, key, value)
                    # Distinguish an intentional baseline jump from absent
                    # source numbers: only numbers missing on disk count as gaps.
                    if watermark is not None and latest.sequence_number is not None:
                        cursor = watermark + 1
                        present = sorted({item.sequence_number for item in scan.observations
                                          if item.sequence_number is not None
                                          and cursor <= item.sequence_number <= latest.sequence_number})
                        for number in present:
                            if number > cursor:
                                self.missing_sequence_ranges.append({
                                    "epoch": self.sequence_epoch, "start": cursor, "end": number - 1,
                                    "recorded_at": scan.scanned_at, "reason": "reinitialization",
                                })
                            cursor = number + 1
                    for item in scan.detections:
                        if item.identity_key in self.processed_image_files:
                            continue
                        self.processed_image_files.add(item.identity_key)
                        self.processed_image_records[item.identity_key] = {
                            **self._detected_image_record(item, frame_seq=None, disposition="backlog"),
                            "reason": "reinitialization", "initialization_generation": generation,
                        }
                    self._advance_sequence_watermark_locked(
                        latest, frame_seq=self.measurement_step_count, record_gap=False,
                    )
                    self._persist_processing_state_locked()
                except Exception:
                    for key, value in previous.items():
                        setattr(self, key, value)
                    raise
        except Exception:
            # This snapshot has not become the active committed marking image.
            snapshot.unlink(missing_ok=True)
            if archive_path is not None:
                archive_path.unlink(missing_ok=True)
            raise

    def confirm_sequence_reset(
        self, run_id: str, candidate_identity_key: str,
    ) -> Dict[str, Any]:
        """Accept an operator-confirmed counter restart without replaying old files."""

        with self._image_scan_lock, self._lock:
            if not self.gsensor_enabled or self._processing_transition_pending:
                raise ValueError("Cannot reset the image sequence while GSensor is disabled.")
            selection = self.experiments.require_current()
            if selection["run_id"] != run_id or not self._experiment_in_progress_locked():
                raise ValueError("Refresh the current active experiment before resetting its sequence.")
            if self.experiment_lifecycle_status == GrowthRateStatus.STOPPING.value:
                raise ValueError("Cannot reset the image sequence while stopping.")
            candidate = self.sequence_reset_candidate
            if candidate is None:
                raise ValueError("No suspected sequence restart is available.")
            if candidate["identity_key"] != candidate_identity_key:
                raise ValueError("The reset candidate changed. Refresh status before confirming.")
            raw_sequence = candidate.get("source_sequence")
            if type(raw_sequence) is not int:
                raise ValueError("The reset candidate has no numeric source sequence.")

            # Existing high-numbered files belong to the previous counter epoch.
            # Ignore their exact revisions, including files that were still pending.
            snapshot = scan_new_images(
                selection["container_image_path"], (),
                minimum_stable_scans=1, image_probe=lambda _path: None,
            )
            if candidate_identity_key not in snapshot.file_identities:
                raise ValueError("The reset image changed or disappeared. Wait for a fresh scan.")
            self.ignored_image_files.update(snapshot.file_identities - {candidate_identity_key})
            self.ignored_image_files.discard(candidate_identity_key)
            self.processed_image_files.discard(candidate_identity_key)
            self.processed_image_records.pop(candidate_identity_key, None)
            self.sequence_epoch += 1
            self.sequence_watermark = raw_sequence - 1
            self.sequence_watermark_record = None
            self.latest_discovered_image = None
            self.latest_ready_image = None
            self.last_late_image = None
            self.sequence_reset_candidate = None
            self.image_observations.clear()
            self.image_discovery_revision += 1
            self.image_scan_error = None
            self._persist_processing_state_locked()
        return self.status()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            current_params = self.current_params()
            initialization_payload = self.initialization.payload()
            initialization_status = initialization_payload.get("status") or self.initialization_status
            if initialization_status == "not_started":
                initialization_status = self.initialization_status
            current_missing_ranges = [
                item for item in self.missing_sequence_ranges
                if item.get("epoch", 0) == self.sequence_epoch
            ]
            missing_sequence_count = sum(
                max(0, int(item.get("end", 0)) - int(item.get("start", 0)) + 1)
                for item in current_missing_ranges
            )
            if self.sequence_reset_candidate is not None:
                sequence_status = "late_or_reset"
            elif self.last_late_image is not None:
                sequence_status = "late_detected"
            elif current_missing_ranges:
                sequence_status = "gaps_detected"
            elif self.sequence_watermark is not None:
                sequence_status = "ok"
            else:
                sequence_status = "waiting"
            return {
                "role": ROLE,
                "active": self.active,
                "gsensor_activation": self._activation_state_locked(),
                "reinitialization": {
                    "can_restart": self._can_restart_initialization_locked(),
                    "in_progress": self._reinitialization_pending,
                    "generation": self.initialization_generation,
                    "completed_count": len(self.initialization_history),
                    "last_error": self.reinitialization_error,
                },
                "last_lifecycle_command_error": self.last_lifecycle_command_error,
                "measurement_running": self._measurement_running(),
                "initialized": self.initialized,
                "initialization_status": initialization_status,
                "initialized_at": self.initialized_at,
                "initialization": initialization_payload,
                "queue": self.queue_name,
                "exchange": self.exchange,
                "param_count": len(current_params),
                "params": current_params,
                "alignment_configuration": self._alignment_configuration_locked(),
                "experiment_parameters": self._experiment_parameters_locked(),
                "last_message": self.last_message,
                "last_command_message": self.last_command_message,
                "last_params_message": self.last_params_message,
                "last_measurement_step_at": self.last_measurement_step_at,
                "measurement_step_count": self.measurement_step_count,
                "growth_rate_processing": {
                    "latest_result": self.last_growth_rate_result,
                    "last_error": self.last_processing_error,
                    "valid_frame_count": self.measurement_valid_frame_count,
                    "invalid_frame_count": self.measurement_invalid_frame_count,
                    "latest_overlay_path": self.latest_overlay_path,
                    "final_overlay_path": self.final_overlay_path,
                },
                "last_dscgr_result": self.last_dscgr_result,
                "baseline": self.baseline,
                "last_status_message": self.last_status_message,
                "last_status_publish_error": self.last_status_publish_error,
                "controller_availability": {
                    "last_message": self.last_controller_status_message,
                    "last_error": self.last_controller_status_publish_error,
                },
                "sample_publishing": {
                    "enabled": self.sample_publisher is not None,
                    "last_message": self.last_sample_message,
                    "last_error": self.last_sample_publish_error,
                    "success_count": self.sample_publish_success_count,
                    "failure_count": self.sample_publish_failure_count,
                },
                "influx_persistence": {
                    "enabled": self.measurement_writer is not None,
                    "last_write_at": self.last_influx_write_at,
                    "last_error": self.last_influx_error,
                    "success_count": self.influx_write_success_count,
                    "failure_count": self.influx_write_failure_count,
                },
                "experiment": self.current_experiment,
                "current_run_id": (
                    self.current_experiment.get("run_id")
                    if self.current_experiment
                    else None
                ),
                "experiment_selection_status": self.experiment_selection_status,
                "experiment_selection_error": self.experiment_selection_error,
                "last_experiment_message": self.last_experiment_message,
                "experiment_lifecycle_status": self.experiment_lifecycle_status,
                "experiment_lifecycle_error": self.experiment_lifecycle_error,
                "experiment_started_at": self.experiment_started_at,
                "experiment_parameter_version": self.experiment_parameter_version,
                "last_lifecycle_message": self.last_lifecycle_message,
                "recovery": {
                    "status": self.recovery_status,
                    "error": self.recovery_error,
                    "state_file": self.processing_state_path,
                },
                "image_scan": {
                    "status": self.image_scan_status,
                    "processed_count": sum(
                        record.get("disposition") not in {"late", "backlog"}
                        for record in self.processed_image_records.values()
                    ),
                    "last_detected_image": self.last_detected_image,
                    "file_modified_at": self.file_modified_at,
                    "detected_at": self.detected_at,
                    "pending_image_count": self.pending_image_count,
                    "last_scan_at": self.last_image_scan_at,
                    "error": self.image_scan_error,
                    "poll_interval_s": self.image_poll_interval_s,
                    "latest_discovered": self.latest_discovered_image,
                    "latest_ready": self.latest_ready_image,
                    "watermark": self.sequence_watermark_record,
                    "sequence_health": {
                        "mode": "filename_sequence",
                        "status": sequence_status,
                        "epoch": self.sequence_epoch,
                        "watermark_sequence": self.sequence_watermark,
                        "missing_count": missing_sequence_count,
                        "missing_ranges": current_missing_ranges,
                        "late_count": self.late_image_count,
                        "last_late": self.last_late_image,
                        "reset_candidate": self.sequence_reset_candidate,
                        "reset_available": self.sequence_reset_candidate is not None
                        and not self._processing_transition_pending
                        and self.experiment_lifecycle_status in {
                            "initializing", "measuring", "waiting_for_initial_image", "baseline_ready",
                        },
                    },
                },
            }

    def current_experiment_image_source(self) -> Dict[str, Any]:
        with self._lock:
            selection = self.experiments.current()
            if selection is None:
                return {
                    "selected": False,
                    "run_id": None,
                    "image_directory": None,
                    "container_image_path": None,
                    "image_count": 0,
                    "first_image": None,
                    "latest_image": None,
                }

            images = list_supported_images(selection["container_image_path"])
            images.sort(key=lambda path: (
                parse_image_sequence(path.name) is not None,
                parse_image_sequence(path.name) or 0,
                path.name.casefold(),
            ))
            return {
                "selected": True,
                "run_id": selection["run_id"],
                "image_directory": selection["image_directory"],
                "container_image_path": selection["container_image_path"],
                "image_count": len(images),
                "first_image": images[0].name if images else None,
                "latest_image": (
                    self.latest_ready_image["image_name"] if self.latest_ready_image else None
                ),
                "latest_discovered_image": images[-1].name if images else None,
            }

    def measurement_overlay_path(self, kind: str, run_id: str | None = None) -> Path:
        if kind not in {"latest", "final"}:
            raise ValueError("Overlay kind must be 'latest' or 'final'.")
        with self._lock:
            selection = self.experiments.require_current()
            if run_id is not None and run_id != selection["run_id"]:
                raise ExperimentNotSelectedError(
                    "The selected experiment changed. Refresh the image for the current experiment."
                )
            configured_path = self.latest_overlay_path if kind == "latest" else self.final_overlay_path
            output_directory = self._measurement_output_directory_locked()
        experiment_directory = Path(selection["container_image_path"]).parent.resolve()
        filename = (
            LATEST_OVERLAY_FILENAME if kind == "latest" else FINAL_OVERLAY_FILENAME
        )
        path = (Path(configured_path) if configured_path else output_directory / filename).resolve()
        try:
            path.relative_to(experiment_directory)
        except ValueError as exc:
            raise ValueError("Overlay path escapes the selected experiment.") from exc
        if not path.is_file():
            raise FileNotFoundError(f"{kind.capitalize()} overlay is not available yet.")
        return path

    def require_active_initialization(self, session_id: str | None = None) -> None:
        with self._lock:
            if self._processing_transition_pending:
                raise ValueError("The marking image is changing. Wait for the new session.")
            if not self.gsensor_enabled:
                raise ValueError("GSensor is disabled; enable it before changing initialization.")
            if self.experiment_lifecycle_status != GrowthRateStatus.INITIALIZING.value:
                raise ValueError(
                    "Initialization changes are only allowed while the experiment is initializing."
                )
            if not session_id or session_id != self.initialization.active_session_id:
                raise ExperimentNotSelectedError(
                    "The initialization session changed. Refresh the page before continuing."
                )

    def apply_initialization_action(
        self,
        session_id: str,
        action: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        with self._lock:
            self.require_active_initialization(session_id)
            return self.persist_initialization_progress(action())

    def persist_initialization_progress(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self._lock:
            self.initialization_status = str(
                payload.get("status") or self.initialization_status
            )
            self._persist_processing_state_locked()
        return payload

    def select_initialization_3d_choice(
        self,
        session_id: str,
        choice: int,
    ) -> Dict[str, Any]:
        """Select one 3D candidate for preview without ending initialization."""

        return self.apply_initialization_action(
            session_id, lambda: self.initialization.select_3d_choice(session_id, choice)
        )

    def confirm_initialization_3d_choice(
        self,
        session_id: str,
        alignment_method: str | None = None,
    ) -> Dict[str, Any]:
        """Freeze the previewed candidate, establish baseline, and measure."""

        with self._control_lock, self._image_scan_lock:
            return self._confirm_initialization_3d_choice(session_id, alignment_method)

    def _confirm_initialization_3d_choice(
        self,
        session_id: str,
        alignment_method: str | None = None,
    ) -> Dict[str, Any]:

        with self._lock:
            self.require_active_initialization(session_id)
            if self.experiment_lifecycle_status != GrowthRateStatus.INITIALIZING.value:
                raise ValueError(
                    "The experiment must be initializing before confirming a 3D candidate."
                )
            selection = self.experiments.require_current()
            payload = self.initialization.payload(session_id)
            if (
                payload.get("status") != "ready_for_3d"
                or payload.get("selected_3d_choice") is None
            ):
                raise ValueError(
                    "Preview and select a 3D candidate before confirming initialization."
                )
            configured = self._alignment_configuration_locked()["configured_method"]
            selected_method = parse_alignment_method(
                configured if alignment_method is None else alignment_method
            ).value
            capability = next(
                (item for item in alignment_capabilities() if item.method == selected_method),
                None,
            )
            if capability is None or not capability.available:
                reason = capability.reason if capability is not None else "not available"
                raise ValueError(f"Alignment method {selected_method!r} is unavailable: {reason}")
            uv_struct_list, kernel = initialize_DSCGR(
                self.initialization,
                session_id=session_id,
            )
            completed_at = utc_ts()
            baseline = self._build_baseline_locked(
                payload,
                uv_struct_list,
                completed_at=completed_at,
            )
            experiment_directory = Path(selection["container_image_path"]).parent
            output_directory = self._measurement_output_directory_locked()
            latest_overlay_path = output_directory / LATEST_OVERLAY_FILENAME
            final_overlay_path = output_directory / FINAL_OVERLAY_FILENAME
            debug_directory = (
                self.dscgr_output_root_path
                / str(selection["run_id"])
                / "hough_debug"
                if self.hough_debug_enabled
                else None
            )
            processor = self.growth_rate_processor_factory(
                run_id=selection["run_id"],
                params=copy.deepcopy(self.experiment_params or self.current_params()),
                uv_struct_list=uv_struct_list,
                kernel=kernel,
                latest_overlay_path=latest_overlay_path,
                final_overlay_path=final_overlay_path,
                debug_directory=debug_directory,
                initial_image_path=payload["selected_image"],
                alignment_method=selected_method,
            )
            # Tracking/Kalman/registration state belongs to the new marking.
            # Only public counters continue so Controller never rejects it as
            # an old frame; algorithm_step and edge histories remain fresh.
            processor.frame_seq = self.measurement_step_count
            processor.valid_frame_count = self.measurement_valid_frame_count
            processor.invalid_frame_count = self.measurement_invalid_frame_count
            configuration = {
                "method": selected_method,
                "configured_method": configured,
                "confirmed": True,
                "confirmed_at": completed_at,
                "revision": self.alignment_revision,
                "effective_after_frame_seq": self.measurement_step_count,
            }
            snapshot = {
                "schema_version": 1,
                "run_id": selection["run_id"],
                "completed_at": completed_at,
                "parameter_version": self.experiment_parameter_version,
                "initialization": payload,
                "baseline": baseline,
                "alignment_configuration": configuration,
                "initialization_generation": self.initialization_generation,
                "frame_seq_base": self.measurement_step_count,
            }
            # A crash between the two writes may leave a prepared, immutable
            # initialization snapshot. Only an uncommitted run may replace it.
            first_confirmation = not self.initialization_history
            if first_confirmation:
                self._discard_uncommitted_initialization_locked(selection["run_id"])
            updates = {
                "initialized": True,
                "initialization_status": str(payload.get("status") or "ready_for_3d"),
                "initialized_at": completed_at,
                "uv_struct_list": processor.uv_structs,
                "kernel": kernel,
                "growth_rate_processor": processor,
                "latest_overlay_path": str(latest_overlay_path),
                "final_overlay_path": None,
                "baseline": baseline,
                "experiment_lifecycle_status": GrowthRateStatus.MEASURING.value,
                "image_scan_status": "baseline_ready",
                "confirmed_alignment_method": selected_method,
                "alignment_confirmed_at": completed_at,
                "alignment_effective_after_frame_seq": self.measurement_step_count,
                "alignment_change_error": None,
                "initialization_history": list(self.initialization_history),
            }
            previous = {key: getattr(self, key) for key in updates}
            new_history_path: Path | None = None
            try:
                if first_confirmation:
                    self.experiments.save_initialization(snapshot)
                    initialization_file = GSENSOR_INITIALIZATION_FILENAME
                else:
                    new_history_path = self.experiments.save_reinitialization_snapshot(
                        selection["run_id"], snapshot, kind="initialization",
                    )
                    initialization_file = new_history_path.relative_to(experiment_directory).as_posix()
                updates["initialization_history"].append({
                    "generation": self.initialization_generation,
                    "session_id": session_id,
                    "initialization_file": initialization_file,
                    "completed_at": completed_at,
                    "frame_seq_base": self.measurement_step_count,
                })
                for key, value in updates.items():
                    setattr(self, key, value)
                # This atomic recovery record commits the confirmation. Nothing
                # may publish readiness or scan images until it is durable.
                self._persist_processing_state_locked()
            except Exception:
                for key, value in previous.items():
                    setattr(self, key, value)
                try:
                    if new_history_path is not None:
                        new_history_path.unlink(missing_ok=True)
                    elif first_confirmation:
                        self._discard_uncommitted_initialization_locked(
                            selection["run_id"], expected_snapshot=snapshot
                        )
                except Exception:
                    logger.exception("Unable to clean up uncommitted initialization snapshot.")
                raise
            image_name = baseline["image_name"]

        self._publish_growth_rate_status(
            GrowthRateStatus.BASELINE_READY,
            frame_seq=self.measurement_step_count,
            image_name=image_name,
        )
        self._publish_growth_rate_status(
            GrowthRateStatus.MEASURING,
            frame_seq=self.measurement_step_count,
            image_name=image_name,
        )
        self.start_image_scanning()
        return {**payload, "baseline": baseline, "alignment_configuration": configuration}

    def _discard_uncommitted_initialization_locked(
        self, run_id: str, *, expected_snapshot: Dict[str, Any] | None = None
    ) -> None:
        path = self.experiments.registry.image_dir(run_id).parent / GSENSOR_INITIALIZATION_FILENAME
        if not path.exists():
            return
        if expected_snapshot is not None:
            # Roll back only the exact file prepared by this failed call, even
            # if Central ended the run while the disk write was in progress.
            if json.loads(path.read_text(encoding="utf-8")) != expected_snapshot:
                return
        elif self.experiments.registry.get(run_id).status in TERMINAL_EXPERIMENT_STATUSES:
            return
        saved = self.experiments.load_processing_state(run_id)
        configuration = (saved or {}).get("alignment_configuration") or {}
        if (
            saved is not None
            and isinstance(configuration, dict)
            and saved.get("lifecycle_status") == GrowthRateStatus.INITIALIZING.value
            and saved.get("processor") is None
            and saved.get("initialized_at") is None
            and not configuration.get("confirmed", False)
        ):
            # Keep the shared manifest's lifecycle untouched; the next save
            # fills the same initialization filename. Completed records stay
            # immutable, even if the current in-memory state is inconsistent.
            path.unlink()

    def _build_baseline_locked(
        self,
        initialization_payload: Dict[str, Any],
        uv_struct_list: list[Any],
        *,
        completed_at: str,
    ) -> Dict[str, Any]:
        image_path = Path(str(initialization_payload["selected_image"]))
        dt_s = float(self.current_params()["dt_G"])
        return {
            "status": "baseline",
            "frame_seq": self.measurement_step_count,
            "segment_frame_seq": 0,
            "initialization_generation": self.initialization_generation,
            "image_name": image_path.name,
            "image_relative_path": image_path.relative_to(
                Path(self.experiments.require_current()["container_image_path"]).parent
            ).as_posix(),
            "file_modified_at": self.file_modified_at,
            "detected_at": self.detected_at,
            "established_at": completed_at,
            "dt_s": dt_s,
            "unit": "m/s",
            "u": {
                "distance_px": 0.0,
                "distance_m": 0.0,
                "G": None,
                "G_KF": None,
                "initial_line": _serialize_initial_line(uv_struct_list[0]),
            },
            "v": {
                "distance_px": 0.0,
                "distance_m": 0.0,
                "G": None,
                "G_KF": None,
                "initial_line": _serialize_initial_line(uv_struct_list[1]),
            },
        }

    def run_dscgr(self, session_id: str | None = None) -> Dict[str, Any]:
        payload = self.initialization.payload(session_id)
        logger.warning(
            "DSCGR service request: session_id=%s status=%s image_folder=%s",
            session_id,
            payload.get("status"),
            payload.get("image_folder"),
        )
        if payload.get("status") != "ready_for_3d":
            raise ValueError("Complete initialization and select a 3D candidate before running DSCGR.")

        uv_struct_list, kernel = initialize_DSCGR(
            self.initialization,
            session_id=session_id,
        )
        logger.warning("DSCGR initialization ready: session_id=%s", session_id)
        output_dir = self.dscgr_output_root_path / uuid4().hex
        result = DSCGR(
            payload["image_folder"],
            self.current_params(),
            uv_struct_list,
            kernel,
            output_dir=output_dir,
        )
        with self._lock:
            self.last_dscgr_result = result
        logger.warning(
            "DSCGR service done: session_id=%s processed_ptrs=%s output_dir=%s",
            session_id,
            result.get("processed_ptrs"),
            result.get("output_dir"),
        )
        return result


service = GsensorService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    try:
        yield
    finally:
        service.close()


web_app = FastAPI(title="Crystallization MPC Gsensor UI", lifespan=lifespan)
web_app.mount("/static/imgs", StaticFiles(directory=GSENSOR_IMGS_DIR), name="gsensor-imgs")
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


@web_app.get("/api/status")
def get_status() -> Dict[str, Any]:
    return service.status()


@web_app.post("/api/image-scan/confirm-sequence-reset")
def confirm_image_sequence_reset(payload: SequenceResetRequest) -> Dict[str, Any]:
    try:
        return service.confirm_sequence_reset(payload.run_id, payload.candidate_identity_key)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.get("/api/measurement/overlay/{kind}")
def get_measurement_overlay(kind: str, run_id: str | None = Query(default=None)) -> FileResponse:
    try:
        path = service.measurement_overlay_path(kind, run_id=run_id)
        return FileResponse(path, media_type="image/jpeg", filename=path.name)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.get("/api/params")
def get_params() -> Dict[str, Any]:
    return service.params_payload()


@web_app.get("/api/alignment/capabilities")
def get_alignment_capabilities() -> Dict[str, Any]:
    return {"methods": [item.to_dict() for item in alignment_capabilities()]}


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, ExperimentNotSelectedError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@web_app.get("/api/initialization/source")
def get_initialization_source() -> Dict[str, Any]:
    try:
        return service.current_experiment_image_source()
    except Exception as exc:
        _raise_http_error(exc)


@web_app.get("/api/initialization/image/{session_id}")
def get_initialization_image(session_id: str) -> FileResponse:
    try:
        path = service.initialization.image_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Image file not found: {path}")
        return FileResponse(
            path,
            media_type=service.initialization.image_media_type(session_id),
            filename=path.name,
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.get("/api/initialization/step")
def get_initialization_step(session_id: str | None = Query(default=None)) -> Dict[str, Any]:
    try:
        return service.initialization.payload(session_id)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/is-full")
def set_initialization_is_full(payload: InitializationIsFullRequest) -> Dict[str, Any]:
    try:
        return service.apply_initialization_action(
            payload.session_id,
            lambda: service.initialization.set_is_full(payload.session_id, payload.is_full)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/point")
def submit_initialization_point(payload: InitializationPointRequest) -> Dict[str, Any]:
    try:
        return service.apply_initialization_action(
            payload.session_id,
            lambda: service.initialization.submit_point(payload.session_id, payload.x, payload.y)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/corner")
def choose_initialization_corner(payload: InitializationCornerRequest) -> Dict[str, Any]:
    try:
        return service.apply_initialization_action(
            payload.session_id,
            lambda: service.initialization.choose_corner(payload.session_id, payload.corner)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/3d-choice")
def choose_initialization_3d_choice(payload: Initialization3DChoiceRequest) -> Dict[str, Any]:
    try:
        return service.select_initialization_3d_choice(payload.session_id, payload.choice)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/confirm")
def confirm_initialization_3d_choice(
    payload: InitializationConfirmRequest,
) -> Dict[str, Any]:
    try:
        return service.confirm_initialization_3d_choice(
            payload.session_id, getattr(payload, "alignment_method", None)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/restart")
def restart_initialization(payload: InitializationRestartRequest) -> Dict[str, Any]:
    try:
        return service.restart_initialization(
            payload.run_id, payload.session_id, payload.control_revision,
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/alignment/select")
def select_alignment_method(payload: AlignmentSelectionRequest) -> Dict[str, Any]:
    try:
        return service.select_alignment_method(
            payload.run_id, payload.session_id, payload.control_revision,
            payload.alignment_revision, payload.alignment_method,
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/undo")
def undo_initialization(payload: InitializationSessionRequest) -> Dict[str, Any]:
    try:
        return service.apply_initialization_action(
            payload.session_id,
            lambda: service.initialization.undo(payload.session_id)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/reset")
def reset_initialization(payload: InitializationResetRequest) -> Dict[str, Any]:
    try:
        return service.apply_initialization_action(
            payload.session_id,
            lambda: service.initialization.reset(payload.session_id)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/dscgr/run")
def run_dscgr(payload: DscgrRunRequest) -> Dict[str, Any]:
    logger.warning("DSCGR API request received: session_id=%s", payload.session_id)
    try:
        return service.run_dscgr(payload.session_id)
    except Exception as exc:
        _raise_http_error(exc)


__all__ = [
    "GsensorService",
    "DscgrRunRequest",
    "Initialization3DChoiceRequest",
    "InitializationConfirmRequest",
    "InitializationCornerRequest",
    "InitializationIsFullRequest",
    "InitializationPointRequest",
    "InitializationResetRequest",
    "InitializationSessionRequest",
    "web_app",
]
