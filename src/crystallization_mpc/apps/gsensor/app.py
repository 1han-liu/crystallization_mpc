from __future__ import annotations

import base64
import binascii
import logging
import os
import re
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
from crystallization_mpc.apps.gsensor.initialization import (
    IMAGE_EXTENSIONS,
    GsensorInitializationManager,
)
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for
from crystallization_mpc.messaging.schema import utc_ts

ROLE = "gsensor"
UI_DIR = Path(__file__).resolve().parent / "ui"
PROJECT_ROOT = UI_DIR.parents[4]
DEFAULT_PARAMS_PATH = PROJECT_ROOT / "params_default.yaml"
DEFAULT_RUNTIME_PARAMS_PATH = PROJECT_ROOT / "params_runtime.yaml"
DEFAULT_PARAM_META_PATH = PROJECT_ROOT / "param_meta.yaml"
DEFAULT_UPLOAD_ROOT = PROJECT_ROOT / ".runtime" / "gsensor_uploads"
logger = logging.getLogger(__name__)


class GsensorParamsUpdate(BaseModel):
    version: int = 1
    params: Dict[str, Any] = Field(default_factory=dict)


class InitializationUploadedFile(BaseModel):
    filename: str
    content_base64: str


class InitializationUploadFolderRequest(BaseModel):
    image_choice: str = "first"
    files: list[InitializationUploadedFile] = Field(default_factory=list)


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


class GsensorService:
    def __init__(
        self,
        url: Optional[str] = None,
        exchange: Optional[str] = None,
        queue_name: Optional[str] = None,
        default_params_path: Optional[str | Path] = None,
        runtime_params_path: Optional[str | Path] = None,
        param_meta_path: Optional[str | Path] = None,
        upload_root_path: Optional[str | Path] = None,
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
        self.upload_root_path = Path(
            upload_root_path
            or os.getenv("GSENSOR_UPLOAD_ROOT", str(DEFAULT_UPLOAD_ROOT))
        )
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
        self.initialization = GsensorInitializationManager()
        self._consumer_thread: threading.Thread | None = None
        self._measurement_thread: threading.Thread | None = None
        self._measurement_stop = threading.Event()
        self._lock = threading.RLock()

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
                self.start_growth_rate()
            elif name == "growth_rate.stop":
                self.stop_growth_rate()

    def prepare_growth_rate_measurement(self) -> None:
        with self._lock:
            self.initialized = True
            self.initialization_status = "placeholder_ready"
            self.initialized_at = utc_ts()

    def start_growth_rate(self) -> None:
        with self._lock:
            if self.active and self._measurement_thread and self._measurement_thread.is_alive():
                return

            self.prepare_growth_rate_measurement()
            self._measurement_stop.clear()
            self.active = True
            self._measurement_thread = threading.Thread(
                target=self._run_measurement_loop,
                name="gsensor-measurement-worker",
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

    def _run_measurement_loop(self) -> None:
        try:
            while not self._measurement_stop.is_set():
                with self._lock:
                    self.measurement_step_count += 1
                    self.last_measurement_step_at = utc_ts()
                self._measurement_stop.wait(1)
        finally:
            with self._lock:
                if self._measurement_stop.is_set():
                    self.active = False

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
            }

    def set_active(self, active: bool) -> Dict[str, Any]:
        if active:
            self.start_growth_rate()
        else:
            self.stop_growth_rate()
        return self.status()

    def start_uploaded_initialization(
        self,
        files: list[InitializationUploadedFile],
        image_choice: str = "first",
    ) -> Dict[str, Any]:
        folder = self._save_uploaded_initialization_folder(files)
        return self.initialization.start_folder(str(folder), image_choice)

    def _save_uploaded_initialization_folder(
        self,
        files: list[InitializationUploadedFile],
    ) -> Path:
        if not files:
            raise ValueError("Select an image folder before loading.")

        folder = self.upload_root_path / uuid4().hex
        folder.mkdir(parents=True, exist_ok=False)
        saved_count = 0
        used_names: set[str] = set()
        for index, uploaded_file in enumerate(files, start=1):
            safe_name = _safe_upload_filename(uploaded_file.filename, index)
            if Path(safe_name).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            target_name = _dedupe_filename(safe_name, used_names)
            try:
                target = folder / target_name
                target.write_bytes(_decode_uploaded_file(uploaded_file.content_base64))
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"Invalid uploaded image data: {uploaded_file.filename}") from exc
            saved_count += 1

        if saved_count == 0:
            raise ValueError("The selected folder does not contain supported image files.")
        return folder


def _safe_upload_filename(filename: str, index: int) -> str:
    name = Path(filename.replace("\\", "/")).name or f"image_{index:05d}.bin"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _dedupe_filename(filename: str, used_names: set[str]) -> str:
    candidate = filename
    path = Path(filename)
    counter = 1
    while candidate in used_names:
        candidate = f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    used_names.add(candidate)
    return candidate


def _decode_uploaded_file(content_base64: str) -> bytes:
    content = content_base64.strip()
    if "," in content and content.startswith("data:"):
        content = content.split(",", 1)[1]
    return base64.b64decode(content, validate=True)


service = GsensorService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    yield


web_app = FastAPI(title="Crystallization MPC Gsensor UI", lifespan=lifespan)
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
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@web_app.post("/api/initialization/upload-folder")
def upload_initialization_folder(payload: InitializationUploadFolderRequest) -> Dict[str, Any]:
    try:
        return service.start_uploaded_initialization(payload.files, payload.image_choice)
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
        return service.initialization.select_3d_choice(payload.session_id, payload.choice)
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


__all__ = [
    "GsensorParamsUpdate",
    "GsensorService",
    "Initialization3DChoiceRequest",
    "InitializationCornerRequest",
    "InitializationIsFullRequest",
    "InitializationPointRequest",
    "InitializationResetRequest",
    "InitializationSessionRequest",
    "InitializationUploadFolderRequest",
    "InitializationUploadedFile",
    "web_app",
]
