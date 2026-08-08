"""Central-facing experiment session management."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from crystallization_mpc.experiments import ExperimentManifest, ExperimentRegistry

CENTRAL_STATE_FILENAME = ".central_experiment_state.json"
WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class CentralExperimentManager:
    """Add persistent Central selection and display paths to the registry."""

    def __init__(
        self,
        root: str | Path,
        *,
        host_root_display: str | None = None,
    ) -> None:
        self.registry = ExperimentRegistry(root)
        display_root = str(host_root_display or self.registry.root).strip()
        if not display_root:
            raise ValueError("Experiment host display root cannot be empty.")
        self.host_root_display = display_root
        self.state_path = self.registry.root / CENTRAL_STATE_FILENAME

    def create(self, *, label: str | None = None) -> dict[str, Any]:
        manifest = self.registry.create(label=label)
        self._save_current_run_id(manifest.run_id)
        return self.describe(manifest, current_run_id=manifest.run_id)

    def list(self) -> dict[str, Any]:
        current_run_id = self.current_run_id()
        return {
            "current_run_id": current_run_id,
            "experiments": [
                self.describe(manifest, current_run_id=current_run_id)
                for manifest in self.registry.list()
            ],
        }

    def get(self, run_id: str) -> dict[str, Any]:
        current_run_id = self.current_run_id()
        return self.describe(
            self.registry.get(run_id),
            current_run_id=current_run_id,
        )

    def select(self, run_id: str) -> dict[str, Any]:
        manifest = self.registry.select(run_id)
        self._save_current_run_id(manifest.run_id)
        return self.describe(manifest, current_run_id=manifest.run_id)

    def start(
        self,
        run_id: str,
        *,
        params_snapshot: Mapping[str, Any],
        parameter_version: int | None = None,
    ) -> dict[str, Any]:
        manifest = self.registry.start(
            run_id,
            params_snapshot=params_snapshot,
            parameter_version=parameter_version,
        )
        return self.describe(manifest, current_run_id=self.current_run_id())

    def finish(self, run_id: str) -> dict[str, Any]:
        manifest = self.registry.finish(run_id)
        return self.describe(manifest, current_run_id=self.current_run_id())

    def current_run_id(self) -> str | None:
        if not self.state_path.exists():
            return None
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not read Central experiment state: {self.state_path}"
            ) from exc
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise RuntimeError(
                f"Invalid Central experiment state: {self.state_path}"
            )
        run_id = document.get("current_run_id")
        if run_id is None:
            return None
        manifest = self.registry.get(str(run_id))
        return manifest.run_id

    def describe(
        self,
        manifest: ExperimentManifest,
        *,
        current_run_id: str | None,
    ) -> dict[str, Any]:
        result = manifest.to_dict()
        result.update(
            {
                "selected": manifest.run_id == current_run_id,
                "camera_save_path": _display_image_path(
                    self.host_root_display,
                    manifest.run_id,
                    manifest.image_directory,
                ),
                "container_image_path": str(
                    self.registry.root
                    / manifest.run_id
                    / manifest.image_directory
                ),
            }
        )
        return result

    def _save_current_run_id(self, run_id: str) -> None:
        manifest = self.registry.get(run_id)
        document = {
            "schema_version": 1,
            "current_run_id": manifest.run_id,
            "selected_at": _utc_iso(),
        }
        _atomic_write_json(self.state_path, document)


def _display_image_path(host_root: str, run_id: str, image_directory: str) -> str:
    if WINDOWS_ABSOLUTE_PATH.match(host_root) or host_root.startswith("\\\\"):
        return str(PureWindowsPath(host_root) / run_id / image_directory)
    return str(PurePosixPath(host_root) / run_id / image_directory)


def _utc_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = ["CENTRAL_STATE_FILENAME", "CentralExperimentManager"]
