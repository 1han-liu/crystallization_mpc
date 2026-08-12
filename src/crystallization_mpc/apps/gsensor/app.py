from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from crystallization_mpc.apps.central.params import (
    load_param_meta,
    load_params,
    save_params_document,
)
from crystallization_mpc.apps.gsensor.DSCGR import DSCGR
from crystallization_mpc.apps.gsensor.detection.initialize_DSCGR import initialize_DSCGR
from crystallization_mpc.apps.gsensor.experiments import (
    ExperimentNotSelectedError,
    GsensorExperimentManager,
)
from crystallization_mpc.apps.gsensor.growth_rate_processor import (
    FINAL_OVERLAY_FILENAME,
    LATEST_OVERLAY_FILENAME,
    GrowthRateFrameResult,
    GrowthRateProcessor,
)
from crystallization_mpc.apps.gsensor.image_watcher import (
    DetectedImage,
    ImageProbe,
    scan_new_images,
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
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.commands import (
    EXPERIMENT_SELECT_COMMAND,
    EXPERIMENT_START_COMMAND,
    EXPERIMENT_STOP_COMMAND,
    GROWTH_RATE_COMPLETED_MESSAGE,
    GROWTH_RATE_SAMPLE_MESSAGE,
    GROWTH_RATE_STATUS_MESSAGE,
    PARAMS_UPDATE_MESSAGE,
)
from crystallization_mpc.messaging.contracts import (
    ExperimentStartPayload,
    ExperimentStopPayload,
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


class GsensorParamsUpdate(BaseModel):
    version: int = 1
    params: Dict[str, Any] = Field(default_factory=dict)


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


class InitializationResetRequest(BaseModel):
    session_id: str | None = None


class DscgrRunRequest(BaseModel):
    session_id: str | None = None


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
        self.experiment_parameter_version: int | None = None
        self.experiment_params: Dict[str, Any] | None = None
        self.last_lifecycle_message: Dict[str, Any] | None = None
        # Values are revision identities (name + mtime_ns + size), not filenames.
        self.processed_image_files: set[str] = set()
        self.processed_image_records: Dict[str, Dict[str, Any]] = {}
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
            should_resume = self.experiment_lifecycle_status in {
                GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value,
                GrowthRateStatus.BASELINE_READY.value,
                GrowthRateStatus.MEASURING.value,
            }
        if should_resume:
            self.start_image_scanning()
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
            if name == EXPERIMENT_START_COMMAND:
                try:
                    self.start_experiment(msg)
                except Exception as exc:
                    with self._lock:
                        self.active = False
                        self.experiment_lifecycle_status = GrowthRateStatus.ERROR.value
                        self.experiment_lifecycle_error = str(exc)
                        self.last_lifecycle_message = msg
                    run_id = msg.get("payload", {}).get("run_id")
                    if isinstance(run_id, str) and run_id.strip():
                        self._publish_growth_rate_status(
                            GrowthRateStatus.ERROR,
                            error=str(exc),
                            run_id=run_id,
                        )
                    logger.warning("Rejected experiment.start command: %s", exc)
            elif name == EXPERIMENT_STOP_COMMAND:
                try:
                    self.stop_experiment(msg)
                except Exception as exc:
                    with self._lock:
                        self.experiment_lifecycle_status = GrowthRateStatus.ERROR.value
                        self.experiment_lifecycle_error = str(exc)
                        self.last_lifecycle_message = msg
                    run_id = msg.get("payload", {}).get("run_id")
                    if isinstance(run_id, str) and run_id.strip():
                        self._publish_growth_rate_status(
                            GrowthRateStatus.ERROR,
                            error=str(exc),
                            run_id=run_id,
                        )
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
        with self._lock:
            selection, changed = self.experiments.select(
                payload,
                running=self.active or self._experiment_in_progress_locked(),
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

    def _reset_experiment_runtime_locked(self) -> None:
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
        self.processed_image_files.clear()
        self.processed_image_records.clear()
        self.last_detected_image = None
        self.file_modified_at = None
        self.detected_at = None
        self.pending_image_count = 0
        self.last_image_scan_at = None
        self.image_scan_status = "stopped"
        self.image_scan_error = None
        self.experiment_started_at = None
        self.experiment_parameter_version = None
        self.experiment_params = None
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
            "schema_version": 1,
            "run_id": self.current_experiment["run_id"],
            "updated_at": utc_ts(),
            "lifecycle_status": self.experiment_lifecycle_status,
            "lifecycle_error": self.experiment_lifecycle_error,
            "experiment_started_at": self.experiment_started_at,
            "parameter_version": self.experiment_parameter_version,
            "algorithm_params": self.experiment_params,
            "initialization": initialization_payload,
            "initialized_at": self.initialized_at,
            "baseline": self.baseline,
            "processed_images": list(self.processed_image_records.values()),
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

        lifecycle_status = str(state.get("lifecycle_status") or "selected")
        allowed_statuses = {
            "selected",
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
        self.experiment_lifecycle_status = lifecycle_status
        self.experiment_lifecycle_error = state.get("lifecycle_error")
        self.experiment_started_at = state.get("experiment_started_at")
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
        self.measurement_step_count = int(
            (self.last_growth_rate_result or {}).get("frame_seq") or 0
        )
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
            experiment_directory = Path(
                self.current_experiment["container_image_path"]
            ).parent
            latest_overlay_path = experiment_directory / LATEST_OVERLAY_FILENAME
            final_overlay_path = experiment_directory / FINAL_OVERLAY_FILENAME
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
            )
            restore_state = getattr(processor, "restore_state", None)
            if not callable(restore_state):
                raise ValueError("Growth-rate processor does not support recovery.")
            restore_state(processor_state)
            self.growth_rate_processor = processor
            self.uv_struct_list = processor.uv_structs
            self.kernel = kernel
            self.initialized = True
            self.latest_overlay_path = str(latest_overlay_path)
            self.final_overlay_path = (
                str(final_overlay_path) if final_overlay_path.is_file() else None
            )

        if restored_records:
            last_record = list(restored_records.values())[-1]
            self.last_detected_image = str(last_record["image_name"])
            self.file_modified_at = last_record.get("file_modified_at")
            self.detected_at = last_record.get("detected_at")
        self.processing_state_path = str(
            self.experiments.processing_state_path(run_id)
        )
        self.recovery_status = "restored"
        self.recovery_error = None

    def start_experiment(self, message: Dict[str, Any]) -> None:
        self._validate_central_lifecycle_message(message)
        payload = ExperimentStartPayload.from_mapping(message.get("payload", {}))
        with self._lock:
            selection = self.experiments.require_current()
            if payload.run_id != selection["run_id"]:
                raise ValueError("experiment.start run_id does not match the selected experiment.")
            if payload.image_directory != selection["image_directory"]:
                raise ValueError(
                    "experiment.start image_directory does not match the selected experiment."
                )
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
            self.processed_image_files.clear()
            self.processed_image_records.clear()
            self.last_detected_image = None
            self.file_modified_at = None
            self.detected_at = None
            self.pending_image_count = 0
            self.measurement_step_count = 0
            self.last_measurement_step_at = None
            self.experiment_started_at = payload.started_at
            self.experiment_parameter_version = payload.parameter_version
            self.experiment_params = self.current_params()
            self.experiment_lifecycle_status = (
                GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
            )
            self.experiment_lifecycle_error = None
            self.last_lifecycle_message = message
            self.recovery_status = "not_needed"
            self.recovery_error = None
            self._persist_processing_state_locked()
        self._publish_growth_rate_status(GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE)
        self.start_image_scanning()

    def stop_experiment(self, message: Dict[str, Any]) -> None:
        self._validate_central_lifecycle_message(message)
        payload = ExperimentStopPayload.from_mapping(message.get("payload", {}))
        with self._lock:
            selection = self.experiments.require_current()
            if payload.run_id != selection["run_id"]:
                raise ValueError("experiment.stop run_id does not match the selected experiment.")
            self.experiment_lifecycle_status = GrowthRateStatus.STOPPING.value
            self.experiment_lifecycle_error = None
            self.last_lifecycle_message = message
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
        self._publish_growth_rate_status(
            GrowthRateStatus.COMPLETED,
            message_name=GROWTH_RATE_COMPLETED_MESSAGE,
        )

    def _experiment_in_progress_locked(self) -> bool:
        return self.experiment_lifecycle_status in {
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
        with self._lock:
            selected_run_id = run_id or (
                self.current_experiment.get("run_id")
                if self.current_experiment
                else None
            )
        if not selected_run_id:
            return None
        status_payload = GrowthRateStatusPayload(
            run_id=str(selected_run_id),
            status=status,
            occurred_at=utc_ts(),
            frame_seq=frame_seq,
            image_name=image_name,
            error=error,
        )
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
        return envelope

    @staticmethod
    def _sample_payload(result: GrowthRateFrameResult) -> GrowthRateSamplePayload:
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
            **values,
        )

    def _publish_growth_rate_sample(
        self,
        result: GrowthRateFrameResult,
    ) -> Dict[str, Any]:
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
        previous_thread: threading.Thread | None
        with self._lock:
            if self.active and self._measurement_thread and self._measurement_thread.is_alive():
                return
            previous_thread = self._measurement_thread

        if (
            previous_thread
            and previous_thread.is_alive()
            and previous_thread is not threading.current_thread()
        ):
            previous_thread.join(timeout=2)

        with self._lock:
            self.experiments.require_current()
            self._measurement_stop.clear()
            self.active = True
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
                self._scan_current_image_directory()
                self._measurement_stop.wait(self.image_poll_interval_s)
        finally:
            with self._lock:
                if self._measurement_stop.is_set():
                    self.active = False
                    if self.experiment_lifecycle_status == GrowthRateStatus.INITIALIZING.value:
                        self.image_scan_status = "paused_for_initialization"
                    else:
                        self.image_scan_status = "stopped"

    def _scan_current_image_directory(self) -> None:
        with self._lock:
            selection = self.experiments.require_current()
            processed_before = set(self.processed_image_files)
            lifecycle_status = self.experiment_lifecycle_status
            processor = self.growth_rate_processor

        try:
            result = scan_new_images(
                selection["container_image_path"],
                processed_before,
                image_probe=self.image_probe,
            )
        except Exception as exc:
            with self._lock:
                self.last_image_scan_at = utc_ts()
                self.image_scan_status = "error"
                self.image_scan_error = str(exc)
            logger.warning("Gsensor image scan failed: %s", exc)
            return

        if (
            lifecycle_status == GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
            and result.detections
        ):
            first = result.detections[0]
            image_path = Path(selection["container_image_path"]) / first.image_name
            try:
                initialization_payload = self.initialization.start_image(image_path)
            except Exception as exc:
                with self._lock:
                    self.active = False
                    self._measurement_stop.set()
                    self.last_image_scan_at = result.scanned_at
                    self.image_scan_status = "error"
                    self.image_scan_error = str(exc)
                    self.experiment_lifecycle_status = GrowthRateStatus.ERROR.value
                    self.experiment_lifecycle_error = str(exc)
                self._publish_growth_rate_status(
                    GrowthRateStatus.ERROR,
                    image_name=first.image_name,
                    error=str(exc),
                )
                return

            with self._lock:
                if (
                    self.experiment_lifecycle_status
                    != GrowthRateStatus.WAITING_FOR_INITIAL_IMAGE.value
                ):
                    return
                self.processed_image_files = processed_before | {first.identity_key}
                self.processed_image_records[first.identity_key] = (
                    self._detected_image_record(first, frame_seq=0)
                )
                self.pending_image_count = (
                    result.pending_image_count + max(0, len(result.detections) - 1)
                )
                self.last_image_scan_at = result.scanned_at
                self.image_scan_error = result.last_error
                self.last_detected_image = first.image_name
                self.file_modified_at = first.file_modified_at
                self.detected_at = first.detected_at
                self.initialized = False
                self.initialization_status = str(
                    initialization_payload.get("status") or "awaiting_is_full"
                )
                self.experiment_lifecycle_status = GrowthRateStatus.INITIALIZING.value
                self.experiment_lifecycle_error = None
                self.image_scan_status = "paused_for_initialization"
                self.active = False
                self._measurement_stop.set()
                self._persist_processing_state_locked()
            self._publish_growth_rate_status(
                GrowthRateStatus.INITIALIZING,
                frame_seq=0,
                image_name=first.image_name,
            )
            return

        if lifecycle_status == GrowthRateStatus.MEASURING.value:
            self._process_detected_images(
                selection,
                result.detections,
                processor,
                processed_before=processed_before,
                scan_pending_count=result.pending_image_count,
                scanned_at=result.scanned_at,
                scan_error=result.last_error,
            )
            return

        with self._lock:
            self.processed_image_files = set(result.processed_files)
            self.pending_image_count = result.pending_image_count
            self.last_image_scan_at = result.scanned_at
            self.image_scan_error = result.last_error
            self.image_scan_status = (
                "running" if result.detections else "waiting_for_image"
            )
            if result.detections:
                latest = result.detections[-1]
                self.last_detected_image = latest.image_name
                self.file_modified_at = latest.file_modified_at
                self.detected_at = latest.detected_at
                self.last_measurement_step_at = latest.detected_at
                self.measurement_step_count += len(result.detections)
            self._persist_processing_state_locked()

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
        processed = set(processed_before)
        remaining = len(detections)

        if processor is None:
            error = "Growth-rate processor is not initialized."
            with self._lock:
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
            image_path = Path(selection["container_image_path"]) / detection.image_name
            frame_result = processor.process(
                image_path,
                captured_at=detection.file_modified_at,
                detected_at=detection.detected_at,
            )
            processed.add(detection.identity_key)
            remaining -= 1
            self._record_processed_frame(detection, frame_result)
            self._publish_growth_rate_sample(frame_result)
            self._persist_growth_rate_result(frame_result)
            self._publish_growth_rate_status(
                GrowthRateStatus.MEASURING,
                frame_seq=frame_result.frame_seq,
                image_name=frame_result.image_name,
            )

        with self._lock:
            self.processed_image_files = processed
            self.pending_image_count = scan_pending_count + remaining
            self.last_image_scan_at = scanned_at
            self.image_scan_error = scan_error
            if detections and len(detections) != remaining:
                self.image_scan_status = "running"
            elif self._measurement_stop.is_set():
                self.image_scan_status = "stopped"
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
            self.processed_image_records[detection.identity_key] = (
                self._detected_image_record(
                    detection,
                    frame_seq=result.frame_seq,
                )
            )
            self.last_detected_image = detection.image_name
            self.file_modified_at = detection.file_modified_at
            self.detected_at = detection.detected_at
            self.last_measurement_step_at = result.processed_at
            self.measurement_step_count = result.frame_seq
            self.last_growth_rate_result = payload
            self.last_processing_error = result.error
            self.latest_overlay_path = result.overlay_path
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
        frame_seq: int,
    ) -> Dict[str, Any]:
        return {
            "identity_key": detection.identity_key,
            "image_name": detection.image_name,
            "modified_time_ns": detection.modified_time_ns,
            "file_size": detection.file_size,
            "file_modified_at": detection.file_modified_at,
            "detected_at": detection.detected_at,
            "frame_seq": int(frame_seq),
        }

    def _measurement_running(self) -> bool:
        return self._measurement_thread is not None and self._measurement_thread.is_alive()

    def _active_params_path(self) -> Path:
        if self.params_path.exists():
            return self.params_path
        return self.default_params_path

    def _load_persisted_params(self) -> tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
        return load_params(str(self._active_params_path()))

    def load_param_meta(self) -> Dict[str, Dict[str, Any]]:
        return load_param_meta(str(self.param_meta_path))

    def current_params(self) -> Dict[str, Any]:
        shared, gsensor, _controller, _version = self._load_persisted_params()
        with self._lock:
            return {**shared, **gsensor, **self.params}

    def params_payload(self) -> Dict[str, Any]:
        _shared, _gsensor, _controller, version = self._load_persisted_params()
        return {
            "version": version,
            "params": self.current_params(),
            "meta": self.load_param_meta(),
            "source_file": str(self._active_params_path()),
            "runtime_file": str(self.params_path),
        }

    def default_params_payload(self) -> Dict[str, Any]:
        shared, gsensor, _controller, version = load_params(str(self.default_params_path))
        return {
            "version": version,
            "params": {**shared, **gsensor},
            "meta": self.load_param_meta(),
            "source_file": str(self.default_params_path),
            "runtime_file": str(self.params_path),
        }

    def apply_ui_params(self, params: Dict[str, Any], version: int = 1) -> Dict[str, Any]:
        shared, gsensor, controller, current_version = self._load_persisted_params()

        shared_keys = set(shared)
        gsensor_keys = set(gsensor)
        if not shared_keys or not gsensor_keys:
            default_shared, default_gsensor, _default_controller, _default_version = load_params(
                str(self.default_params_path)
            )
            shared_keys.update(default_shared)
            gsensor_keys.update(default_gsensor)

        next_shared = dict(shared)
        next_gsensor = dict(gsensor)
        for key, value in params.items():
            if key in shared_keys:
                next_shared[key] = value
            elif key in gsensor_keys:
                next_gsensor[key] = value
            else:
                # Unknown gsensor-facing keys should still be preserved for the
                # local service without being mixed into controller parameters.
                next_gsensor[key] = value

        save_params_document(
            str(self.params_path),
            version=version or current_version,
            shared=next_shared,
            gsensor=next_gsensor,
            controller=controller,
        )

        with self._lock:
            self.params.update(params)

        return self.params_payload()

    def reset_ui_params_to_default(self) -> Dict[str, Any]:
        default_shared, default_gsensor, _default_controller, default_version = load_params(
            str(self.default_params_path)
        )
        _shared, _gsensor, controller, _current_version = self._load_persisted_params()
        save_params_document(
            str(self.params_path),
            version=default_version,
            shared=default_shared,
            gsensor=default_gsensor,
            controller=controller,
        )

        with self._lock:
            self.params = {
                key: value
                for key, value in self.params.items()
                if key not in default_shared and key not in default_gsensor
            }
            self.params.update(default_shared)
            self.params.update(default_gsensor)

        return self.params_payload()

    def status(self) -> Dict[str, Any]:
        current_params = self.current_params()
        initialization_payload = self.initialization.payload()
        initialization_status = initialization_payload.get("status") or self.initialization_status
        if initialization_status == "not_started":
            initialization_status = self.initialization_status
        with self._lock:
            return {
                "role": ROLE,
                "active": self.active,
                "measurement_running": self._measurement_running(),
                "initialized": self.initialized,
                "initialization_status": initialization_status,
                "initialized_at": self.initialized_at,
                "initialization": initialization_payload,
                "queue": self.queue_name,
                "exchange": self.exchange,
                "param_count": len(current_params),
                "params": current_params,
                "last_message": self.last_message,
                "last_command_message": self.last_command_message,
                "last_params_message": self.last_params_message,
                "last_measurement_step_at": self.last_measurement_step_at,
                "measurement_step_count": self.measurement_step_count,
                "growth_rate_processing": {
                    "latest_result": self.last_growth_rate_result,
                    "last_error": self.last_processing_error,
                    "valid_frame_count": (
                        self.growth_rate_processor.valid_frame_count
                        if self.growth_rate_processor is not None
                        else 0
                    ),
                    "invalid_frame_count": (
                        self.growth_rate_processor.invalid_frame_count
                        if self.growth_rate_processor is not None
                        else 0
                    ),
                    "latest_overlay_path": self.latest_overlay_path,
                    "final_overlay_path": self.final_overlay_path,
                },
                "last_dscgr_result": self.last_dscgr_result,
                "baseline": self.baseline,
                "last_status_message": self.last_status_message,
                "last_status_publish_error": self.last_status_publish_error,
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
                    "processed_count": len(self.processed_image_files),
                    "last_detected_image": self.last_detected_image,
                    "file_modified_at": self.file_modified_at,
                    "detected_at": self.detected_at,
                    "pending_image_count": self.pending_image_count,
                    "last_scan_at": self.last_image_scan_at,
                    "error": self.image_scan_error,
                    "poll_interval_s": self.image_poll_interval_s,
                },
            }

    def current_experiment_image_source(self) -> Dict[str, Any]:
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
        return {
            "selected": True,
            "run_id": selection["run_id"],
            "image_directory": selection["image_directory"],
            "container_image_path": selection["container_image_path"],
            "image_count": len(images),
            "first_image": images[0].name if images else None,
            "latest_image": images[-1].name if images else None,
        }

    def measurement_overlay_path(self, kind: str) -> Path:
        if kind not in {"latest", "final"}:
            raise ValueError("Overlay kind must be 'latest' or 'final'.")
        with self._lock:
            selection = self.experiments.require_current()
        experiment_directory = Path(selection["container_image_path"]).parent.resolve()
        filename = (
            LATEST_OVERLAY_FILENAME if kind == "latest" else FINAL_OVERLAY_FILENAME
        )
        path = (experiment_directory / filename).resolve()
        try:
            path.relative_to(experiment_directory)
        except ValueError as exc:
            raise ValueError("Overlay path escapes the selected experiment.") from exc
        if not path.is_file():
            raise FileNotFoundError(f"{kind.capitalize()} overlay is not available yet.")
        return path

    def require_active_initialization(self) -> None:
        with self._lock:
            if self.experiment_lifecycle_status != GrowthRateStatus.INITIALIZING.value:
                raise ValueError(
                    "Initialization changes are only allowed while the experiment is initializing."
                )

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

        self.require_active_initialization()
        return self.persist_initialization_progress(
            self.initialization.select_3d_choice(session_id, choice)
        )

    def confirm_initialization_3d_choice(
        self,
        session_id: str,
    ) -> Dict[str, Any]:
        """Freeze the previewed candidate, establish baseline, and measure."""

        with self._lock:
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
            latest_overlay_path = experiment_directory / LATEST_OVERLAY_FILENAME
            final_overlay_path = experiment_directory / FINAL_OVERLAY_FILENAME
            debug_directory = (
                self.dscgr_output_root_path
                / str(selection["run_id"])
                / "hough_debug"
                if self.hough_debug_enabled
                else None
            )
            processor = self.growth_rate_processor_factory(
                run_id=selection["run_id"],
                params=self.experiment_params or self.current_params(),
                uv_struct_list=uv_struct_list,
                kernel=kernel,
                latest_overlay_path=latest_overlay_path,
                final_overlay_path=final_overlay_path,
                debug_directory=debug_directory,
            )
            snapshot = {
                "schema_version": 1,
                "run_id": selection["run_id"],
                "completed_at": completed_at,
                "parameter_version": self.experiment_parameter_version,
                "initialization": payload,
                "baseline": baseline,
            }
            self.experiments.save_initialization(snapshot)
            self.initialized = True
            self.initialization_status = str(payload.get("status") or "ready_for_3d")
            self.initialized_at = completed_at
            self.uv_struct_list = processor.uv_structs
            self.kernel = kernel
            self.growth_rate_processor = processor
            self.latest_overlay_path = str(latest_overlay_path)
            self.final_overlay_path = None
            self.baseline = baseline
            self.experiment_lifecycle_status = GrowthRateStatus.BASELINE_READY.value
            self.image_scan_status = "baseline_ready"
            image_name = baseline["image_name"]

        self._publish_growth_rate_status(
            GrowthRateStatus.BASELINE_READY,
            frame_seq=0,
            image_name=image_name,
        )
        with self._lock:
            self.experiment_lifecycle_status = GrowthRateStatus.MEASURING.value
            self._persist_processing_state_locked()
        self._publish_growth_rate_status(
            GrowthRateStatus.MEASURING,
            frame_seq=0,
            image_name=image_name,
        )
        self.start_image_scanning()
        return {**payload, "baseline": baseline}

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
            "frame_seq": 0,
            "image_name": image_path.name,
            "image_relative_path": f"images/{image_path.name}",
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


@web_app.get("/api/status")
def get_status() -> Dict[str, Any]:
    return service.status()


@web_app.get("/api/measurement/overlay/{kind}")
def get_measurement_overlay(kind: str) -> FileResponse:
    try:
        path = service.measurement_overlay_path(kind)
        return FileResponse(path, media_type="image/jpeg", filename=path.name)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.get("/api/params")
def get_params() -> Dict[str, Any]:
    return service.params_payload()


@web_app.post("/api/params")
def update_params(payload: GsensorParamsUpdate) -> Dict[str, Any]:
    return service.apply_ui_params(payload.params, payload.version)


@web_app.post("/api/params/reset")
def reset_params() -> Dict[str, Any]:
    return service.reset_ui_params_to_default()


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
        service.require_active_initialization()
        return service.persist_initialization_progress(
            service.initialization.set_is_full(payload.session_id, payload.is_full)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/point")
def submit_initialization_point(payload: InitializationPointRequest) -> Dict[str, Any]:
    try:
        service.require_active_initialization()
        return service.persist_initialization_progress(
            service.initialization.submit_point(payload.session_id, payload.x, payload.y)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/corner")
def choose_initialization_corner(payload: InitializationCornerRequest) -> Dict[str, Any]:
    try:
        service.require_active_initialization()
        return service.persist_initialization_progress(
            service.initialization.choose_corner(payload.session_id, payload.corner)
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
    payload: InitializationSessionRequest,
) -> Dict[str, Any]:
    try:
        return service.confirm_initialization_3d_choice(payload.session_id)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/undo")
def undo_initialization(payload: InitializationSessionRequest) -> Dict[str, Any]:
    try:
        service.require_active_initialization()
        return service.persist_initialization_progress(
            service.initialization.undo(payload.session_id)
        )
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/reset")
def reset_initialization(payload: InitializationResetRequest) -> Dict[str, Any]:
    try:
        service.require_active_initialization()
        return service.persist_initialization_progress(
            service.initialization.reset(payload.session_id)
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
    "GsensorParamsUpdate",
    "GsensorService",
    "DscgrRunRequest",
    "Initialization3DChoiceRequest",
    "InitializationCornerRequest",
    "InitializationIsFullRequest",
    "InitializationPointRequest",
    "InitializationResetRequest",
    "InitializationSessionRequest",
    "web_app",
]
