from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from crystallization_mpc.apps.central.params import (
    load_param_meta,
    load_params,
    save_params_document,
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
logger = logging.getLogger(__name__)


class GsensorParamsUpdate(BaseModel):
    version: int = 1
    params: Dict[str, Any] = Field(default_factory=dict)


class GsensorService:
    def __init__(
        self,
        url: Optional[str] = None,
        exchange: Optional[str] = None,
        queue_name: Optional[str] = None,
        default_params_path: Optional[str | Path] = None,
        runtime_params_path: Optional[str | Path] = None,
        param_meta_path: Optional[str | Path] = None,
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
        with self._lock:
            return {
                "role": ROLE,
                "active": self.active,
                "measurement_running": self._measurement_running(),
                "initialized": self.initialized,
                "initialization_status": self.initialization_status,
                "initialized_at": self.initialized_at,
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


__all__ = ["GsensorParamsUpdate", "GsensorService", "web_app"]
