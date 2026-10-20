"""Test-only HTTP process generator. Bind to the isolated test network only."""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .opcua_client import ProsysConfig, ProsysProcessClient
from .plant import PlantService


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1)
    experiment_started_at: str


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1)
    experiment_started_at: str | None = None
    seed: int = Field(default=123, ge=0, lt=2**32, strict=True)
    parameters: dict[str, float] = Field(default_factory=dict)
    acknowledge_reset: bool = False


class SeedRequest(Identity):
    event_id: str = Field(min_length=1)
    mass_kg: float = .001


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1)
    experiment_started_at: str | None


def create_app(service: PlantService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        actual = service or PlantService(
            ProsysProcessClient(ProsysConfig.from_env()),
            Path(os.getenv("PROSYS_TEST_STATE_FILE", "/data/plant-state.json")),
        )
        app.state.plant = actual
        actual.start_worker()
        try:
            yield
        finally:
            actual.close()

    app = FastAPI(title="Prosys External Test Plant (Synthetic)", lifespan=lifespan)

    def invoke(request: Request, name: str, **kwargs: Any) -> Any:
        try:
            return getattr(request.app.state.plant, name)(**kwargs)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"service": "prosys-test-plant", "source": "Synthetic process, not hardware"}

    @app.get("/status")
    def status(request: Request) -> dict[str, Any]:
        return request.app.state.plant.status()

    @app.post("/reset")
    def reset(payload: ResetRequest, request: Request) -> dict[str, Any]:
        return invoke(request, "reset", **payload.model_dump())

    @app.post("/start")
    def start(payload: Identity, request: Request) -> dict[str, Any]:
        return invoke(request, "start", **payload.model_dump())

    @app.post("/seed")
    def seed(payload: SeedRequest, request: Request) -> dict[str, Any]:
        return invoke(request, "add_seed", **payload.model_dump())

    @app.post("/stop")
    def stop(payload: StopRequest, request: Request) -> dict[str, Any]:
        return invoke(request, "stop", **payload.model_dump())

    return app


web_app = create_app()


__all__ = ["create_app", "web_app"]
