"""Filesystem-backed experiment-session registry."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from crystallization_mpc.experiments.models import (
    ExperimentManifest,
    ExperimentStatus,
)

MANIFEST_FILENAME = "experiment.json"
PARAMS_SNAPSHOT_FILENAME = "params_snapshot.yaml"
GSENSOR_INITIALIZATION_FILENAME = "gsensor_initialization.json"
IMAGE_DIRECTORY_NAME = "images"
RUN_ID_PATTERN = re.compile(
    r"^exp_\d{8}T\d{6}_\d{3}Z_[0-9a-f]{6}$",
    flags=re.ASCII,
)
INVALID_LABEL_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ExperimentRegistryError(RuntimeError):
    """Base error for experiment-session persistence."""


class InvalidExperimentIdentifierError(ExperimentRegistryError, ValueError):
    """Raised when a run ID or label is unsafe or invalid."""


class ExperimentNotFoundError(ExperimentRegistryError, FileNotFoundError):
    """Raised when an experiment cannot be found."""


class InvalidExperimentStateError(ExperimentRegistryError):
    """Raised when a lifecycle transition is not allowed."""


class ExperimentSnapshotExistsError(ExperimentRegistryError, FileExistsError):
    """Raised when an immutable snapshot would be overwritten."""


class ExperimentRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        label: str | None = None,
        now: datetime | None = None,
    ) -> ExperimentManifest:
        normalized_label = _normalize_label(label)
        created_at = _utc_iso(now)

        for _attempt in range(10):
            run_id = _new_run_id(now)
            run_dir = self._resolve_run_dir(run_id)
            try:
                run_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                continue

            image_dir = run_dir / IMAGE_DIRECTORY_NAME
            image_dir.mkdir(parents=False, exist_ok=False)
            manifest = ExperimentManifest(
                run_id=run_id,
                label=normalized_label,
                created_at=created_at,
            )
            self._write_manifest(manifest)
            return manifest

        raise ExperimentRegistryError("Could not allocate a unique experiment run_id.")

    def list(self) -> list[ExperimentManifest]:
        manifests: list[ExperimentManifest] = []
        for child in self.root.iterdir():
            if not child.is_dir() or not RUN_ID_PATTERN.fullmatch(child.name):
                continue
            manifest_path = child / MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            manifests.append(self._read_manifest(manifest_path, expected_run_id=child.name))
        return sorted(manifests, key=lambda item: item.created_at, reverse=True)

    def get(self, run_id: str) -> ExperimentManifest:
        run_dir = self._resolve_existing_run_dir(run_id)
        manifest_path = run_dir / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise ExperimentNotFoundError(f"Experiment manifest not found: {run_id}")
        return self._read_manifest(manifest_path, expected_run_id=run_id)

    def select(self, run_id: str) -> ExperimentManifest:
        """Validate and return an experiment without mutating persistent state."""

        return self.get(run_id)

    def image_dir(self, run_id: str) -> Path:
        manifest = self.get(run_id)
        image_dir = self._resolve_existing_run_dir(run_id) / manifest.image_directory
        if not image_dir.is_dir():
            raise ExperimentNotFoundError(f"Experiment image directory not found: {run_id}")
        return image_dir

    def start(
        self,
        run_id: str,
        *,
        params_snapshot: Mapping[str, Any],
        parameter_version: int | None = None,
        now: datetime | None = None,
    ) -> ExperimentManifest:
        manifest = self.get(run_id)
        if manifest.status == ExperimentStatus.RUNNING:
            self._write_yaml_snapshot_once(
                self._resolve_existing_run_dir(run_id) / PARAMS_SNAPSHOT_FILENAME,
                params_snapshot,
            )
            return manifest
        if manifest.status != ExperimentStatus.CREATED:
            raise InvalidExperimentStateError(
                f"Cannot start experiment {run_id} from state {manifest.status.value}."
            )

        self._write_yaml_snapshot_once(
            self._resolve_existing_run_dir(run_id) / PARAMS_SNAPSHOT_FILENAME,
            params_snapshot,
        )
        resolved_version = _parameter_version(params_snapshot, parameter_version)
        updated = manifest.updated(
            status=ExperimentStatus.RUNNING,
            started_at=_utc_iso(now),
            parameter_version=resolved_version,
            params_snapshot_file=PARAMS_SNAPSHOT_FILENAME,
            error=None,
        )
        self._write_manifest(updated)
        return updated

    def finish(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> ExperimentManifest:
        manifest = self.get(run_id)
        if manifest.status == ExperimentStatus.COMPLETED:
            return manifest
        if manifest.status == ExperimentStatus.FAILED:
            raise InvalidExperimentStateError(
                f"Cannot complete failed experiment {run_id}."
            )

        updated = manifest.updated(
            status=ExperimentStatus.COMPLETED,
            ended_at=_utc_iso(now),
        )
        self._write_manifest(updated)
        return updated

    def mark_failed(
        self,
        run_id: str,
        *,
        error: str,
        now: datetime | None = None,
    ) -> ExperimentManifest:
        manifest = self.get(run_id)
        if manifest.status == ExperimentStatus.COMPLETED:
            raise InvalidExperimentStateError(
                f"Cannot fail completed experiment {run_id}."
            )
        message = str(error).strip()
        if not message:
            raise ValueError("Experiment failure error is required.")

        updated = manifest.updated(
            status=ExperimentStatus.FAILED,
            ended_at=_utc_iso(now),
            error=message,
        )
        self._write_manifest(updated)
        return updated

    def save_gsensor_initialization(
        self,
        run_id: str,
        initialization: Mapping[str, Any],
    ) -> ExperimentManifest:
        manifest = self.get(run_id)
        if manifest.status in {ExperimentStatus.COMPLETED, ExperimentStatus.FAILED}:
            raise InvalidExperimentStateError(
                f"Cannot save initialization for experiment {run_id} "
                f"in state {manifest.status.value}."
            )

        payload = dict(initialization)
        payload_run_id = payload.get("run_id")
        if payload_run_id is not None and str(payload_run_id) != run_id:
            raise ValueError("Gsensor initialization run_id does not match experiment.")
        payload["run_id"] = run_id
        snapshot_path = (
            self._resolve_existing_run_dir(run_id) / GSENSOR_INITIALIZATION_FILENAME
        )
        self._write_json_snapshot_once(snapshot_path, payload)

        updated = manifest.updated(
            gsensor_initialization_file=GSENSOR_INITIALIZATION_FILENAME
        )
        self._write_manifest(updated)
        return updated

    def _resolve_run_dir(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(str(run_id)):
            raise InvalidExperimentIdentifierError(f"Invalid experiment run_id: {run_id!r}")
        candidate = (self.root / run_id).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise InvalidExperimentIdentifierError(
                f"Experiment path escapes configured root: {run_id!r}"
            ) from exc
        return candidate

    def _resolve_existing_run_dir(self, run_id: str) -> Path:
        run_dir = self._resolve_run_dir(run_id)
        if not run_dir.is_dir():
            raise ExperimentNotFoundError(f"Experiment not found: {run_id}")
        return run_dir

    def _manifest_path(self, run_id: str) -> Path:
        return self._resolve_run_dir(run_id) / MANIFEST_FILENAME

    def _write_manifest(self, manifest: ExperimentManifest) -> None:
        manifest_path = self._manifest_path(manifest.run_id)
        _atomic_write_text(
            manifest_path,
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

    def _read_manifest(self, path: Path, *, expected_run_id: str) -> ExperimentManifest:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExperimentRegistryError(f"Could not read experiment manifest: {path}") from exc
        if not isinstance(data, dict):
            raise ExperimentRegistryError(f"Experiment manifest must be an object: {path}")
        try:
            manifest = ExperimentManifest.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise ExperimentRegistryError(f"Invalid experiment manifest: {path}") from exc
        if manifest.run_id != expected_run_id:
            raise ExperimentRegistryError(
                f"Experiment manifest run_id does not match directory: {path}"
            )
        return manifest

    def _write_yaml_snapshot_once(
        self,
        path: Path,
        payload: Mapping[str, Any],
    ) -> None:
        document = dict(payload)
        if path.exists():
            try:
                existing = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise ExperimentRegistryError(f"Could not read parameter snapshot: {path}") from exc
            if existing != document:
                raise ExperimentSnapshotExistsError(
                    f"Parameter snapshot is immutable and already exists: {path}"
                )
            return

        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        _atomic_write_text(path, text)

    def _write_json_snapshot_once(self, path: Path, payload: Mapping[str, Any]) -> None:
        document = dict(payload)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExperimentRegistryError(
                    f"Could not read Gsensor initialization snapshot: {path}"
                ) from exc
            if existing != document:
                raise ExperimentSnapshotExistsError(
                    f"Gsensor initialization snapshot is immutable and already exists: {path}"
                )
            return

        _atomic_write_text(
            path,
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        )


def _new_run_id(now: datetime | None) -> str:
    current = _utc_datetime(now)
    timestamp = current.strftime("%Y%m%dT%H%M%S_%f")[:19]
    return f"exp_{timestamp}Z_{uuid4().hex[:6]}"


def _utc_iso(now: datetime | None) -> str:
    current = _utc_datetime(now)
    return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_datetime(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Experiment timestamps must be timezone-aware.")
    return current.astimezone(timezone.utc)


def _normalize_label(label: str | None) -> str | None:
    if label is None:
        return None
    normalized = str(label).strip()
    if not normalized:
        return None
    if len(normalized) > 120:
        raise InvalidExperimentIdentifierError("Experiment label must be 120 characters or fewer.")
    if INVALID_LABEL_CHARACTERS.search(normalized):
        raise InvalidExperimentIdentifierError(
            "Experiment label contains unsupported filename characters."
        )
    return normalized


def _parameter_version(
    params_snapshot: Mapping[str, Any],
    explicit_version: int | None,
) -> int | None:
    value = explicit_version if explicit_version is not None else params_snapshot.get("version")
    if value is None:
        return None
    return int(value)


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "GSENSOR_INITIALIZATION_FILENAME",
    "IMAGE_DIRECTORY_NAME",
    "MANIFEST_FILENAME",
    "PARAMS_SNAPSHOT_FILENAME",
    "ExperimentNotFoundError",
    "ExperimentRegistry",
    "ExperimentRegistryError",
    "ExperimentSnapshotExistsError",
    "InvalidExperimentIdentifierError",
    "InvalidExperimentStateError",
]
