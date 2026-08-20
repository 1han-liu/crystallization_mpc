"""Validated, persistent run configuration owned by Central."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

RUN_CONFIGURATION_SCHEMA_VERSION = 1
RUN_CONFIGURATION_FILENAME = ".central_run_configuration.json"

RUN_TYPES = ("experiment", "simulation")
CONTROLLER_MODES = ("MPC", "PI")
CONTROL_TARGETS = ("sigma", "G")
ADAPTATION_MODES = (
    "E_A",
    "k_0",
    "n",
    "E_A_and_k_0",
    "E_A_and_n",
    "k_0_and_n",
    "all",
)
GROWTH_RATE_SOURCES = ("live_gsensor", "simulated", "presaved_images")


@dataclass(frozen=True)
class RunConfiguration:
    """Configuration selected before one experiment or simulation starts."""

    run_type: str = "experiment"
    controller_mode: str = "MPC"
    control_target: str = "sigma"
    adaptation_enabled: bool = False
    adaptation_mode: str = "E_A"
    growth_rate_source: str = "live_gsensor"

    def __post_init__(self) -> None:
        _require_choice(self.run_type, "run_type", RUN_TYPES)
        _require_choice(self.controller_mode, "controller_mode", CONTROLLER_MODES)
        _require_choice(self.control_target, "control_target", CONTROL_TARGETS)
        if not isinstance(self.adaptation_enabled, bool):
            raise ValueError("adaptation_enabled must be true or false.")
        _require_choice(self.adaptation_mode, "adaptation_mode", ADAPTATION_MODES)
        _require_choice(
            self.growth_rate_source,
            "growth_rate_source",
            GROWTH_RATE_SOURCES,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_type": self.run_type,
            "controller_mode": self.controller_mode,
            "control_target": self.control_target,
            "adaptation_enabled": self.adaptation_enabled,
            "adaptation_mode": self.adaptation_mode,
            "growth_rate_source": self.growth_rate_source,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunConfiguration":
        if not isinstance(value, Mapping):
            raise ValueError("Run configuration must be an object.")
        expected = {
            "run_type",
            "controller_mode",
            "control_target",
            "adaptation_enabled",
            "adaptation_mode",
            "growth_rate_source",
        }
        unknown = sorted(set(value) - expected)
        if unknown:
            raise ValueError(
                f"Unknown run configuration field(s): {', '.join(unknown)}."
            )
        missing = sorted(expected - set(value))
        if missing:
            raise ValueError(
                f"Missing run configuration field(s): {', '.join(missing)}."
            )
        return cls(
            run_type=_required_text(value["run_type"], "run_type"),
            controller_mode=_required_text(
                value["controller_mode"], "controller_mode"
            ),
            control_target=_required_text(value["control_target"], "control_target"),
            adaptation_enabled=value["adaptation_enabled"],
            adaptation_mode=_required_text(
                value["adaptation_mode"], "adaptation_mode"
            ),
            growth_rate_source=_required_text(
                value["growth_rate_source"], "growth_rate_source"
            ),
        )


class RunConfigurationStore:
    """Atomically persist the latest pre-run configuration."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.updated_at: str | None = None

    def load(self, default: RunConfiguration) -> RunConfiguration:
        if not self.path.is_file():
            self.updated_at = None
            return default
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not read Central run configuration: {self.path}"
            ) from exc
        if not isinstance(document, dict):
            raise RuntimeError("Central run configuration document must be an object.")
        if int(document.get("schema_version", 0)) != RUN_CONFIGURATION_SCHEMA_VERSION:
            raise RuntimeError("Unsupported Central run configuration schema.")
        configuration = document.get("configuration")
        if not isinstance(configuration, Mapping):
            raise RuntimeError("Central run configuration is missing configuration.")
        self.updated_at = _optional_text(document.get("updated_at"))
        return RunConfiguration.from_mapping(configuration)

    def save(self, configuration: RunConfiguration) -> str:
        updated_at = _utc_iso()
        document = {
            "schema_version": RUN_CONFIGURATION_SCHEMA_VERSION,
            "updated_at": updated_at,
            "configuration": configuration.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        self.updated_at = updated_at
        return updated_at


def _required_text(value: Any, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} is required.")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} is required.")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_choice(value: str, name: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        allowed = ", ".join(choices)
        raise ValueError(f"{name} must be one of: {allowed}.")


def _utc_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "ADAPTATION_MODES",
    "CONTROLLER_MODES",
    "CONTROL_TARGETS",
    "GROWTH_RATE_SOURCES",
    "RUN_CONFIGURATION_FILENAME",
    "RUN_TYPES",
    "RunConfiguration",
    "RunConfigurationStore",
]
