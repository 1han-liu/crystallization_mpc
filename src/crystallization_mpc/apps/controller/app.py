"""HTTP entry point for the Controller container."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from crystallization_mpc.apps.controller.service import ControllerService


service = ControllerService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    try:
        yield
    finally:
        service.stop()


web_app = FastAPI(title="Crystallization MPC Controller", lifespan=lifespan)


@web_app.get("/")
def index() -> dict[str, str]:
    return {
        "service": "crystallization-mpc-controller",
        "status_url": "/api/status",
    }


@web_app.get("/api/status")
def get_status() -> dict[str, Any]:
    return service.status()


__all__ = ["service", "web_app"]
