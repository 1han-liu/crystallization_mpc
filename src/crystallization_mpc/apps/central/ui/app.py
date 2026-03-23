from __future__ import annotations

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
DEFAULT_PARAM_META_PATH = PROJECT_ROOT / "param_meta.yaml"


class TargetUpdate(BaseModel):
    target: Literal["sigma", "G"]


class ParamsUpdate(BaseModel):
    version: int = 1
    shared: Dict[str, Any] = Field(default_factory=dict)
    gsensor: Dict[str, Any] = Field(default_factory=dict)
    controller: Dict[str, Any] = Field(default_factory=dict)


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

    def connect(self) -> None:
        if self._ch is not None:
            return
        binding_keys = bindings_for(ROLE, include_broadcast=self.include_broadcast)
        self._conn, self._ch = connect(self.url)
        declare_exchange(self._ch, self.exchange)
        declare_queue(self._ch, self.queue_name, binding_keys, self.exchange)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None
        self._ch = None

    def _require_channel(self):
        if self._ch is None:
            raise RuntimeError("CentralApp is not connected. Call connect() first.")
        return self._ch

    def publish_params(
        self,
        shared: Dict[str, object],
        gsensor: Dict[str, object],
        controller: Dict[str, object],
        version: int,
    ) -> None:
        ch = self._require_channel()
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
            publish(ch, self.exchange, route(ROLE, "gsensor"), env, persistent=True)

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
            publish(ch, self.exchange, route(ROLE, "controller"), env, persistent=True)

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
        self.params_path = Path(os.getenv("PARAMS_FILE", str(DEFAULT_PARAMS_PATH)))
        self.param_meta_path = Path(os.getenv("PARAM_META_FILE", str(DEFAULT_PARAM_META_PATH)))
        self.target: Literal["sigma", "G"] = os.getenv("CONTROL_TARGET", "sigma")  # type: ignore[assignment]
        if self.target not in TARGET_VALUES:
            self.target = "sigma"
        self.publisher = CentralApp()

    def start(self) -> None:
        self.publisher.connect()

    def stop(self) -> None:
        self.publisher.close()

    def load_params(self) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
        return load_params(str(self.params_path))

    def load_param_meta(self) -> Dict[str, Dict[str, Any]]:
        return load_param_meta(str(self.param_meta_path))

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
            params_path=str(self.params_path),
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
        "preview": preview,
    }


@web_app.post("/api/operation/target")
def update_target(payload: TargetUpdate) -> Dict[str, Any]:
    service.target = payload.target
    return {
        "saved": True,
        "target": service.target,
    }


@web_app.post("/api/operation/publish")
def publish_operation() -> Dict[str, Any]:
    try:
        return service.publish()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


__all__ = ["CentralApp", "web_app"]
