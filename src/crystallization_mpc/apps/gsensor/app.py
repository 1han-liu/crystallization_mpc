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

from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for
from crystallization_mpc.messaging.schema import utc_ts

ROLE = "gsensor"
UI_DIR = Path(__file__).resolve().parent / "ui"
logger = logging.getLogger(__name__)


class GsensorService:
    def __init__(
        self,
        url: Optional[str] = None,
        exchange: Optional[str] = None,
        queue_name: Optional[str] = None,
    ) -> None:
        self.url = url or os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")
        self.exchange = exchange or os.getenv("RABBIT_EXCHANGE", EXCHANGE)
        self.queue_name = queue_name or os.getenv("RABBIT_QUEUE", QUEUES[ROLE])
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

    def status(self) -> Dict[str, Any]:
        with self._lock:
            params = dict(self.params)
            return {
                "role": ROLE,
                "active": self.active,
                "measurement_running": self._measurement_running(),
                "initialized": self.initialized,
                "initialization_status": self.initialization_status,
                "initialized_at": self.initialized_at,
                "queue": self.queue_name,
                "exchange": self.exchange,
                "param_count": len(params),
                "params": params,
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


__all__ = ["GsensorService", "web_app"]
