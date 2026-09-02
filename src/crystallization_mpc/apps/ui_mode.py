from __future__ import annotations

import os
from typing import Literal

UiMode = Literal["development", "production"]

DEVELOPMENT_UI_MODE: UiMode = "development"
PRODUCTION_UI_MODE: UiMode = "production"
UI_MODE_ENV = "UI_MODE"


def resolve_ui_mode(value: str | None = None) -> UiMode:
    """Return the configured UI mode, failing closed to production."""
    configured = os.getenv(UI_MODE_ENV, PRODUCTION_UI_MODE) if value is None else value
    normalized = str(configured).strip().lower()
    if normalized == DEVELOPMENT_UI_MODE:
        return DEVELOPMENT_UI_MODE
    return PRODUCTION_UI_MODE


def ui_mode_payload(mode: str) -> dict[str, str | bool]:
    resolved = resolve_ui_mode(mode)
    return {
        "mode": resolved,
        "development": resolved == DEVELOPMENT_UI_MODE,
    }


__all__ = [
    "DEVELOPMENT_UI_MODE",
    "PRODUCTION_UI_MODE",
    "UI_MODE_ENV",
    "UiMode",
    "resolve_ui_mode",
    "ui_mode_payload",
]
