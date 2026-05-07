from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from crystallization_mpc.apps.central.params import (
    apply_derived_params,
    load_operation_meta,
    load_param_meta,
    load_params,
    save_params_document,
)
from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.publisher import publish
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue
from crystallization_mpc.messaging.idgen import next_seq
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for, route
from crystallization_mpc.messaging.schema import build_envelope

ROLE = "central"
TARGET_VALUES = ("sigma", "G")
UI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = UI_DIR.parents[4]
DEFAULT_PARAMS_PATH = PROJECT_ROOT / "params_default.yaml"
DEFAULT_RUNTIME_PARAMS_PATH = PROJECT_ROOT / "params_runtime.yaml"
DEFAULT_PARAM_META_PATH = PROJECT_ROOT / "param_meta.yaml"
DEFAULT_OPERATION_META_PATH = PROJECT_ROOT / "operation_meta.yaml"
logger = logging.getLogger(__name__)


class TargetUpdate(BaseModel):
    target: Literal["sigma", "G"]


class ParamsUpdate(BaseModel):
    version: int = 1
    shared: Dict[str, Any] = Field(default_factory=dict)
    gsensor: Dict[str, Any] = Field(default_factory=dict)
    controller: Dict[str, Any] = Field(default_factory=dict)


class OperationValueUpdate(BaseModel):
    key: str
    value: Any


class OperationActionUpdate(BaseModel):
    key: str


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
    ) -> None:
        seq = next_seq()

        if shared or gsensor:
            payload = {"version": version, "params": {**shared, **gsensor}}
            env = build_envelope(
                src=ROLE,
                dst="gsensor",
                msg_type="params",
                name="update",
                seq=seq,
                payload=payload,
            )
            self._publish_with_reconnect(route(ROLE, "gsensor"), env, persistent=True)

        if shared or controller:
            payload = {"version": version, "params": {**shared, **controller}}
            env = build_envelope(
                src=ROLE,
                dst="controller",
                msg_type="params",
                name="update",
                seq=seq,
                payload=payload,
            )
            self._publish_with_reconnect(route(ROLE, "controller"), env, persistent=True)

    def build_growth_rate_command(self, active: bool, seq: Optional[int] = None) -> Dict[str, Any]:
        command_name = "growth_rate.start" if active else "growth_rate.stop"
        return build_envelope(
            src=ROLE,
            dst="gsensor",
            msg_type="command",
            name=command_name,
            seq=next_seq() if seq is None else seq,
            payload={"key": "G_active", "active": active},
        )

    def publish_growth_rate_command(self, active: bool) -> Dict[str, Any]:
        env = self.build_growth_rate_command(active)
        self._publish_with_reconnect(route(ROLE, "gsensor"), env, persistent=True)
        return env

    def load_and_publish(
        self,
        params_path: Optional[str] = None,
        target: Optional[str] = None,
    ) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int, Dict[str, object]]:
        path = params_path or os.getenv("PARAMS_FILE", str(DEFAULT_PARAMS_PATH))
        shared, gsensor, controller, version = load_params(path)
        shared, controller, derived = apply_derived_params(shared, controller, target=target)
        self.publish_params(shared, gsensor, controller, version)
        return shared, gsensor, controller, version, derived


class CentralService:
    def __init__(self) -> None:
        self.default_params_path = Path(os.getenv("PARAMS_DEFAULT_FILE", str(DEFAULT_PARAMS_PATH)))
        self.params_path = Path(os.getenv("PARAMS_FILE", str(DEFAULT_RUNTIME_PARAMS_PATH)))
        self.param_meta_path = Path(os.getenv("PARAM_META_FILE", str(DEFAULT_PARAM_META_PATH)))
        self.operation_meta_path = Path(os.getenv("OPERATION_META_FILE", str(DEFAULT_OPERATION_META_PATH)))
        self.target: Literal["sigma", "G"] = os.getenv("CONTROL_TARGET", "sigma")  # type: ignore[assignment]
        if self.target not in TARGET_VALUES:
            self.target = "sigma"
        self.publisher = CentralApp()
        self.operation_state = self._build_default_operation_state()

    def _active_params_path(self) -> Path:
        if self.params_path.exists():
            return self.params_path
        return self.default_params_path

    def start(self) -> None:
        self.publisher.connect()

    def stop(self) -> None:
        self.publisher.close()

    def load_params(self) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
        return load_params(str(self._active_params_path()))

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
        self.operation_state[key] = value
        if key == "target" and value in TARGET_VALUES:
            self.target = value
            self.operation_state["target"] = value
        return {
            "saved": True,
            "key": key,
            "value": self.operation_state.get(key),
        }

    def trigger_operation_action(self, key: str) -> Dict[str, Any]:
        current = bool(self.operation_state.get(key, False))
        if key == "G_active":
            active = not current
            self.publisher.publish_growth_rate_command(active)
            self.operation_state[key] = active
            return {
                "triggered": True,
                "key": key,
                "value": active,
                "placeholder": True,
            }

        if key in {"controller_active", "adaptive", "inline_display"}:
            self.operation_state[key] = not current
            return {
                "triggered": True,
                "key": key,
                "value": self.operation_state[key],
                "placeholder": True,
            }
        self.operation_state[key] = True
        return {
            "triggered": True,
            "key": key,
            "value": True,
            "placeholder": True,
        }

    def save_params(self, payload: ParamsUpdate) -> None:
        save_params_document(
            str(self.params_path),
            version=payload.version,
            shared=payload.shared,
            gsensor=payload.gsensor,
            controller=payload.controller,
        )

    def preview_publish_payload(self) -> Dict[str, Any]:
        shared, gsensor, controller, version = self.load_params()
        shared, controller, derived = apply_derived_params(shared, controller, target=self.target)
        return {
            "version": version,
            "target": self.target,
            "shared": shared,
            "gsensor": gsensor,
            "controller": controller,
            "derived": derived,
        }

    def publish(self) -> Dict[str, Any]:
        shared, gsensor, controller, version, derived = self.publisher.load_and_publish(
            params_path=str(self._active_params_path()),
            target=self.target,
        )
        return {
            "version": version,
            "target": self.target,
            "shared": shared,
            "gsensor": gsensor,
            "controller": controller,
            "derived": derived,
            "published": True,
        }

    def start_growth_rate(self) -> Dict[str, Any]:
        command = self.publisher.publish_growth_rate_command(True)
        self.operation_state["G_active"] = True
        return {
            "triggered": True,
            "key": "G_active",
            "value": True,
            "command": command,
        }

    def stop_growth_rate(self) -> Dict[str, Any]:
        command = self.publisher.publish_growth_rate_command(False)
        self.operation_state["G_active"] = False
        return {
            "triggered": True,
            "key": "G_active",
            "value": False,
            "command": command,
        }


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


@web_app.get("/api/params")
def get_params() -> Dict[str, Any]:
    shared, gsensor, controller, version = service.load_params()
    return {
        "version": version,
        "shared": shared,
        "gsensor": gsensor,
        "controller": controller,
        "meta": service.load_param_meta(),
        "source_file": str(service._active_params_path()),
    }


@web_app.post("/api/params")
def update_params(payload: ParamsUpdate) -> Dict[str, Any]:
    service.save_params(payload)
    return {
        "saved": True,
        "version": payload.version,
    }


@web_app.get("/api/operation/state")
def get_operation_state() -> Dict[str, Any]:
    preview = service.preview_publish_payload()
    return {
        "target": service.target,
        "state": service.operation_state,
        "preview": preview,
    }


@web_app.get("/api/operation/meta")
def get_operation_meta() -> Dict[str, Any]:
    return {
        "sections": service.load_operation_meta(),
    }


@web_app.post("/api/operation/target")
def update_target(payload: TargetUpdate) -> Dict[str, Any]:
    service.target = payload.target
    service.operation_state["target"] = payload.target
    return {
        "saved": True,
        "target": service.target,
    }


@web_app.post("/api/operation/value")
def update_operation_value(payload: OperationValueUpdate) -> Dict[str, Any]:
    return service.update_operation_value(payload.key, payload.value)


@web_app.post("/api/operation/action")
def trigger_operation_action(payload: OperationActionUpdate) -> Dict[str, Any]:
    return service.trigger_operation_action(payload.key)


@web_app.post("/api/operation/growth-rate/start")
def start_growth_rate(payload: Optional[ParamsUpdate] = None) -> Dict[str, Any]:
    try:
        return service.start_growth_rate()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@web_app.post("/api/operation/growth-rate/stop")
def stop_growth_rate() -> Dict[str, Any]:
    try:
        return service.stop_growth_rate()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@web_app.post("/api/operation/publish")
def publish_operation() -> Dict[str, Any]:
    try:
        return service.publish()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


__all__ = ["CentralApp", "web_app"]
