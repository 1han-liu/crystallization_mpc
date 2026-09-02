"""Persistent experiment selection for the Gsensor service."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from crystallization_mpc.experiments import ExperimentRegistry
from crystallization_mpc.messaging.commands import EXPERIMENT_MODE_LIVE

GSENSOR_STATE_FILENAME = ".gsensor_experiment_state.json"
GSENSOR_PROCESSING_STATE_FILENAME = "gsensor_processing_state.json"
IMAGE_DIRECTORY_NAME = "images"


class InvalidExperimentSelectionError(ValueError):
    """Raised when an experiment.select payload is invalid."""


class ExperimentSwitchWhileRunningError(RuntimeError):
    """Raised when a running Gsensor is asked to switch experiments."""


class ExperimentNotSelectedError(RuntimeError):
    """Raised when an operation requires a selected experiment."""


class GsensorExperimentManager:
    def __init__(self, root: str | Path) -> None:
        self.registry = ExperimentRegistry(root)
        self.state_path = self.registry.root / GSENSOR_STATE_FILENAME

    def current(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidExperimentSelectionError(
                f"Could not read Gsensor experiment state: {self.state_path}"
            ) from exc
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise InvalidExperimentSelectionError(
                f"Invalid Gsensor experiment state: {self.state_path}"
            )

        run_id = _required_text(document, "run_id")
        image_directory = _required_text(document, "image_directory")
        mode = _required_text(document, "mode")
        selected_at = _required_text(document, "selected_at")
        return self._validated_selection(
            run_id=run_id,
            image_directory=image_directory,
            mode=mode,
            selected_at=selected_at,
        )

    def require_current(self) -> dict[str, Any]:
        current = self.current()
        if current is None:
            raise ExperimentNotSelectedError(
                "No experiment is selected. Create or select an experiment in Central first."
            )
        return current

    def select(
        self,
        payload: Mapping[str, Any],
        *,
        running: bool,
    ) -> tuple[dict[str, Any], bool]:
        if not isinstance(payload, Mapping):
            raise InvalidExperimentSelectionError(
                "experiment.select payload must be an object."
            )
        run_id = _required_text(payload, "run_id")
        image_directory = _required_text(payload, "image_directory")
        mode = _required_text(payload, "mode")
        current = self.current()

        if running and (current is None or current["run_id"] != run_id):
            raise ExperimentSwitchWhileRunningError(
                "Cannot switch experiments while growth-rate measurement is running."
            )

        if current is not None and current["run_id"] == run_id:
            selection = self._validated_selection(
                run_id=run_id,
                image_directory=image_directory,
                mode=mode,
                selected_at=current["selected_at"],
            )
            return selection, False

        selection = self._validated_selection(
            run_id=run_id,
            image_directory=image_directory,
            mode=mode,
            selected_at=_utc_iso(),
        )
        self._write_state(selection)
        return selection, True

    def save_initialization(self, initialization: Mapping[str, Any]) -> dict[str, Any]:
        current = self.require_current()
        manifest = self.registry.save_gsensor_initialization(
            current["run_id"],
            initialization,
        )
        return manifest.to_dict()

    def load_initialization(self, run_id: str) -> dict[str, Any]:
        manifest = self.registry.get(run_id)
        filename = manifest.gsensor_initialization_file
        if not filename:
            raise FileNotFoundError(
                f"Gsensor initialization is not available for {run_id}."
            )
        path = self.registry.image_dir(run_id).parent / filename
        return _read_json_object(path, "Gsensor initialization")

    def load_processing_state(self, run_id: str) -> dict[str, Any] | None:
        path = self.processing_state_path(run_id)
        if not path.is_file():
            return None
        document = _read_json_object(path, "Gsensor processing state")
        if int(document.get("schema_version", 0)) != 1:
            raise InvalidExperimentSelectionError(
                f"Unsupported Gsensor processing-state schema: {path}"
            )
        if str(document.get("run_id") or "") != run_id:
            raise InvalidExperimentSelectionError(
                "Gsensor processing-state run_id does not match its experiment."
            )
        return document

    def save_processing_state(
        self,
        run_id: str,
        state: Mapping[str, Any],
    ) -> Path:
        current = self.registry.get(run_id)
        document = dict(state)
        if int(document.get("schema_version", 0)) != 1:
            raise ValueError("Gsensor processing state schema_version must be 1.")
        if str(document.get("run_id") or "") != current.run_id:
            raise ValueError("Gsensor processing state run_id does not match experiment.")
        path = self.processing_state_path(run_id)
        _atomic_write_json(path, document)
        return path

    def processing_state_path(self, run_id: str) -> Path:
        return self.registry.image_dir(run_id).parent / GSENSOR_PROCESSING_STATE_FILENAME

    def _validated_selection(
        self,
        *,
        run_id: str,
        image_directory: str,
        mode: str,
        selected_at: str,
    ) -> dict[str, Any]:
        if image_directory != IMAGE_DIRECTORY_NAME:
            raise InvalidExperimentSelectionError(
                "experiment.select image_directory must be 'images'."
            )
        if mode != EXPERIMENT_MODE_LIVE:
            raise InvalidExperimentSelectionError(
                "experiment.select mode must be 'live'."
            )
        manifest = self.registry.get(run_id)
        if manifest.image_directory != image_directory:
            raise InvalidExperimentSelectionError(
                "experiment.select image_directory does not match the experiment manifest."
            )
        image_path = self.registry.image_dir(run_id)
        return {
            "run_id": manifest.run_id,
            "image_directory": image_directory,
            "mode": mode,
            "container_image_path": str(image_path),
            "selected_at": selected_at,
        }

    def _write_state(self, selection: Mapping[str, Any]) -> None:
        document = {
            "schema_version": 1,
            "run_id": selection["run_id"],
            "image_directory": selection["image_directory"],
            "mode": selection["mode"],
            "selected_at": selection["selected_at"],
        }
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        finally:
            if temporary.exists():
                temporary.unlink()


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidExperimentSelectionError(
            f"experiment.select requires a non-empty string field: {key}."
        )
    return value.strip()


def _utc_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidExperimentSelectionError(
            f"Could not read {description}: {path}"
        ) from exc
    if not isinstance(document, dict):
        raise InvalidExperimentSelectionError(
            f"{description} must be a JSON object: {path}"
        )
    return document


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                dict(document),
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "GSENSOR_STATE_FILENAME",
    "GSENSOR_PROCESSING_STATE_FILENAME",
    "ExperimentNotSelectedError",
    "ExperimentSwitchWhileRunningError",
    "GsensorExperimentManager",
    "InvalidExperimentSelectionError",
]
