"""HTTP entry point for the Controller container."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, HTTPException, Body

from crystallization_mpc.apps.controller.service import ControllerService
from crystallization_mpc.infra.numeric import encode_nonfinite


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
    return encode_nonfinite(service.status())


@web_app.get("/api/runtime-history")
def get_runtime_history(run_id: str, before: int | None = Query(None, ge=1),
                        after: int | None = Query(None, ge=0), limit: int = Query(50, ge=1, le=100)):
    if before is not None and after is not None:
        raise HTTPException(status_code=422, detail="Use either before or after, not both.")
    return service.runtime_history_page(run_id, before, after, limit)


@web_app.post("/api/process/recovery/confirm")
def confirm_process_recovery(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return service.confirm_process_recovery(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


__all__ = ["service", "web_app"]
