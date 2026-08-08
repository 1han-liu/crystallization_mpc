from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional
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
from crystallization_mpc.apps.gsensor.initialization import (
    GsensorInitializationManager,
    list_supported_images,
)
from crystallization_mpc.apps.gsensor.image_watcher import (
    ImageProbe,
    scan_new_images,
)
from crystallization_mpc.apps.gsensor.experiments import (
    ExperimentNotSelectedError,
    GsensorExperimentManager,
)
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.commands import EXPERIMENT_SELECT_COMMAND
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for
from crystallization_mpc.messaging.schema import utc_ts

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


class InitializationWhileRunningError(RuntimeError):
    """Raised when initialization is requested during active measurement."""


class GsensorParamsUpdate(BaseModel):
    version: int = 1
    params: Dict[str, Any] = Field(default_factory=dict)


class InitializationStartRequest(BaseModel):
    image_choice: str = "first"


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
        self.initialization = GsensorInitializationManager()
        self.experiments = GsensorExperimentManager(self.experiment_root_path)
        self.current_experiment: Dict[str, Any] | None = None
        self.experiment_selection_status = "not_selected"
        self.experiment_selection_error: str | None = None
        self.last_experiment_message: Dict[str, Any] | None = None
        self.processed_image_files: set[str] = set()
        self.last_detected_image: str | None = None
        self.file_modified_at: str | None = None
        self.detected_at: str | None = None
        self.pending_image_count = 0
        self.last_image_scan_at: str | None = None
        self.image_scan_status = "stopped"
        self.image_scan_error: str | None = None
        self._consumer_thread: threading.Thread | None = None
        self._measurement_thread: threading.Thread | None = None
        self._measurement_stop = threading.Event()
        self._lock = threading.RLock()
        try:
            self.current_experiment = self.experiments.current()
            if self.current_experiment is not None:
                self.experiment_selection_status = "selected"
        except Exception as exc:
            self.experiment_selection_status = "error"
            self.experiment_selection_error = str(exc)
            logger.exception("Could not restore Gsensor experiment selection.")

    def start(self) -> None:
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._consumer_thread = threading.Thread(
            target=self._consume_forever,
            name="gsensor-rabbitmq-consumer",
            daemon=True,
        )
        self._consumer_thread.start()

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
        if msg.get("msg_type") == "params" and msg.get("name") == "update":
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
            if name == "growth_rate.start":
                try:
                    self.start_growth_rate()
                except Exception as exc:
                    with self._lock:
                        self.active = False
                        self.image_scan_status = "error"
                        self.image_scan_error = str(exc)
                    logger.warning("Rejected growth_rate.start command: %s", exc)
            elif name == "growth_rate.stop":
                self.stop_growth_rate()
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
                running=self.active,
            )
            if changed:
                self._reset_experiment_runtime_locked()
            self.current_experiment = selection
            self.experiment_selection_status = "selected"
            self.experiment_selection_error = None
            self.last_experiment_message = message
            return dict(selection)

    def _reset_experiment_runtime_locked(self) -> None:
        self.initialized = False
        self.initialization_status = "not_initialized"
        self.initialized_at = None
        self.initialization = GsensorInitializationManager()
        self.last_measurement_step_at = None
        self.measurement_step_count = 0
        self.last_dscgr_result = None
        self._measurement_stop.clear()
        self._measurement_thread = None
        self.processed_image_files.clear()
        self.last_detected_image = None
        self.file_modified_at = None
        self.detected_at = None
        self.pending_image_count = 0
        self.last_image_scan_at = None
        self.image_scan_status = "stopped"
        self.image_scan_error = None

    def start_growth_rate(self) -> None:
        with self._lock:
            if self.active and self._measurement_thread and self._measurement_thread.is_alive():
                return

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

    def stop_growth_rate(self) -> None:
        thread: threading.Thread | None
        with self._lock:
            self.active = False
            self._measurement_stop.set()
            thread = self._measurement_thread

        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

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
                    self.image_scan_status = "stopped"

    def _scan_current_image_directory(self) -> None:
        with self._lock:
            selection = self.experiments.require_current()
            processed_before = set(self.processed_image_files)

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
                "last_dscgr_result": self.last_dscgr_result,
                "experiment": self.current_experiment,
                "current_run_id": (
                    self.current_experiment.get("run_id")
                    if self.current_experiment
                    else None
                ),
                "experiment_selection_status": self.experiment_selection_status,
                "experiment_selection_error": self.experiment_selection_error,
                "last_experiment_message": self.last_experiment_message,
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

    def set_active(self, active: bool) -> Dict[str, Any]:
        if active:
            self.start_growth_rate()
        else:
            self.stop_growth_rate()
        return self.status()

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

    def start_current_experiment_initialization(
        self,
        image_choice: str = "first",
    ) -> Dict[str, Any]:
        with self._lock:
            if self.active:
                raise InitializationWhileRunningError(
                    "Stop growth-rate measurement before starting initialization."
                )
            selection = self.experiments.require_current()
            payload = self.initialization.start_folder(
                selection["container_image_path"],
                image_choice,
            )
            self.initialized = False
            self.initialization_status = str(payload.get("status") or "not_initialized")
            self.initialized_at = None
        return payload

    def select_initialization_3d_choice(
        self,
        session_id: str,
        choice: int,
    ) -> Dict[str, Any]:
        with self._lock:
            selection = self.experiments.require_current()
            payload = self.initialization.select_3d_choice(session_id, choice)
            snapshot = {**payload, "run_id": selection["run_id"]}
            self.experiments.save_initialization(snapshot)
            self.initialized = True
            self.initialization_status = str(payload.get("status") or "ready_for_3d")
            if self.initialized_at is None:
                self.initialized_at = utc_ts()
        return payload

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
    yield


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
    if isinstance(exc, InitializationWhileRunningError):
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


@web_app.post("/api/initialization/start")
def start_current_experiment_initialization(
    payload: InitializationStartRequest,
) -> Dict[str, Any]:
    try:
        return service.start_current_experiment_initialization(payload.image_choice)
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


@web_app.post("/api/initialization/is-full")
def set_initialization_is_full(payload: InitializationIsFullRequest) -> Dict[str, Any]:
    try:
        return service.initialization.set_is_full(payload.session_id, payload.is_full)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.get("/api/initialization/step")
def get_initialization_step(session_id: str | None = Query(default=None)) -> Dict[str, Any]:
    try:
        return service.initialization.payload(session_id)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/point")
def submit_initialization_point(payload: InitializationPointRequest) -> Dict[str, Any]:
    try:
        return service.initialization.submit_point(payload.session_id, payload.x, payload.y)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/corner")
def choose_initialization_corner(payload: InitializationCornerRequest) -> Dict[str, Any]:
    try:
        return service.initialization.choose_corner(payload.session_id, payload.corner)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/3d-choice")
def choose_initialization_3d_choice(payload: Initialization3DChoiceRequest) -> Dict[str, Any]:
    try:
        return service.select_initialization_3d_choice(payload.session_id, payload.choice)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/undo")
def undo_initialization(payload: InitializationSessionRequest) -> Dict[str, Any]:
    try:
        return service.initialization.undo(payload.session_id)
    except Exception as exc:
        _raise_http_error(exc)


@web_app.post("/api/initialization/reset")
def reset_initialization(payload: InitializationResetRequest) -> Dict[str, Any]:
    try:
        return service.initialization.reset(payload.session_id)
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
    "InitializationStartRequest",
    "InitializationWhileRunningError",
    "web_app",
]
