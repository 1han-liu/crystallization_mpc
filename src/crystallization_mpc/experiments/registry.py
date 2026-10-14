"""Filesystem-backed experiment-session registry."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from crystallization_mpc.experiments.models import (
    TERMINAL_EXPERIMENT_STATUSES,
    ExperimentManifest,
    ExperimentStatus,
    require_experiment_transition,
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
MANIFEST_READ_ATTEMPTS = 5
MANIFEST_READ_RETRY_SECONDS = 0.025


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
        # Cache parsed documents, never filesystem observations: every lookup
        # checks current metadata so other services' atomic replacements remain
        # visible. Opening and resolving every historical JSON on each status
        # poll is especially expensive on Docker Desktop bind mounts.
        self._index_lock = threading.RLock()
        self._directory_cache: dict[Path, tuple[int, ...]] = {}
        self._manifest_cache: dict[
            Path, tuple[tuple[int, ...], ExperimentManifest]
        ] = {}

    def create(
        self,
        *,
        label: str | None = None,
        now: datetime | None = None,
    ) -> ExperimentManifest:
        normalized_label = _normalize_label(label)
        current = _utc_datetime(now)
        created_at = _utc_iso(current)

        for _attempt in range(10):
            run_id = _new_run_id(current)
            if any(item.run_id == run_id for item in self.list()):
                continue
            directory_name = _directory_name(current, normalized_label)
            for suffix in range(1, 10001):
                name = directory_name if suffix == 1 else f"{directory_name}_{suffix}"
                run_dir = self.root / name
                try:
                    run_dir.mkdir(parents=False, exist_ok=False)
                except FileExistsError:
                    continue
                break
            else:
                raise ExperimentRegistryError("Could not allocate an experiment directory.")
            image_dir = run_dir / IMAGE_DIRECTORY_NAME
            image_dir.mkdir(parents=False, exist_ok=False)
            manifest = ExperimentManifest(
                run_id=run_id,
                label=normalized_label,
                created_at=created_at,
            )
            _atomic_write_text(
                run_dir / MANIFEST_FILENAME,
                json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
            )
            return manifest

        raise ExperimentRegistryError("Could not allocate a unique experiment run_id.")

    def list(self) -> list[ExperimentManifest]:
        return [manifest for _path, manifest in self.list_with_directories()]

    def list_with_directories(self) -> list[tuple[Path, ExperimentManifest]]:
        """Read a complete, validated index once for history and batch operations."""

        return sorted(self._entries(), key=lambda item: item[1].created_at, reverse=True)

    def run_dir(self, run_id: str) -> Path:
        """Resolve the stable run ID through its manifest, including legacy names."""

        return self._resolve_existing_run_dir(run_id)

    def get(self, run_id: str) -> ExperimentManifest:
        run_dir = self._resolve_existing_run_dir(run_id)
        manifest_path = run_dir / MANIFEST_FILENAME
        try:
            return self._read_manifest(manifest_path, expected_run_id=run_id)
        except ExperimentRegistryError as exc:
            if not manifest_path.is_file():
                raise ExperimentNotFoundError(
                    f"Experiment manifest not found: {run_id}"
                ) from exc
            raise

    def select(self, run_id: str) -> ExperimentManifest:
        """Validate and return an experiment without mutating persistent state."""

        return self.get(run_id)

    def image_dir(self, run_id: str) -> Path:
        manifest = self.get(run_id)
        image_dir = self._resolve_existing_run_dir(run_id) / manifest.image_directory
        self._contained_path(image_dir)
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
        if manifest.status == ExperimentStatus.STARTING:
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
            status=ExperimentStatus.STARTING,
            started_at=_utc_iso(now),
            parameter_version=resolved_version,
            params_snapshot_file=PARAMS_SNAPSHOT_FILENAME,
            error=None,
        )
        self._write_manifest(updated)
        return updated

    def transition(
        self,
        run_id: str,
        status: ExperimentStatus | str,
    ) -> ExperimentManifest:
        manifest = self.get(run_id)
        target = status if isinstance(status, ExperimentStatus) else ExperimentStatus(status)
        if target == manifest.status:
            return manifest
        try:
            require_experiment_transition(manifest.status, target)
        except ValueError as exc:
            raise InvalidExperimentStateError(str(exc)) from exc

        updated = manifest.updated(status=target, error=None)
        self._write_manifest(updated)
        return updated

    def request_stop(self, run_id: str) -> ExperimentManifest:
        manifest = self.get(run_id)
        if manifest.status in TERMINAL_EXPERIMENT_STATUSES:
            return manifest
        if manifest.status == ExperimentStatus.CREATED:
            raise InvalidExperimentStateError(
                f"Cannot stop experiment {run_id} before it has started."
            )
        return self.transition(run_id, ExperimentStatus.STOPPING)

    def finish(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> ExperimentManifest:
        manifest = self.get(run_id)
        if manifest.status == ExperimentStatus.COMPLETED:
            return manifest
        if manifest.status == ExperimentStatus.ERROR:
            raise InvalidExperimentStateError(
                f"Cannot complete experiment {run_id} after an error."
            )

        try:
            require_experiment_transition(manifest.status, ExperimentStatus.COMPLETED)
        except ValueError as exc:
            raise InvalidExperimentStateError(str(exc)) from exc

        updated = manifest.updated(
            status=ExperimentStatus.COMPLETED,
            ended_at=_utc_iso(now),
        )
        self._write_manifest(updated)
        return updated

    def mark_error(
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
            status=ExperimentStatus.ERROR,
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
        if manifest.status in TERMINAL_EXPERIMENT_STATUSES:
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

    def _validate_run_id(self, run_id: str) -> None:
        if not RUN_ID_PATTERN.fullmatch(str(run_id)):
            raise InvalidExperimentIdentifierError(f"Invalid experiment run_id: {run_id!r}")

    def _contained_path(self, path: Path) -> Path:
        candidate = path.resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise InvalidExperimentIdentifierError(
                f"Experiment path escapes configured root: {path}"
            ) from exc
        return candidate

    def _entries(self) -> list[tuple[Path, ExperimentManifest]]:
        with self._index_lock:
            return self._scan_entries()

    def _scan_entries(self) -> list[tuple[Path, ExperimentManifest]]:
        entries: list[tuple[Path, ExperimentManifest]] = []
        seen: set[str] = set()
        directory_cache: dict[Path, tuple[int, ...]] = {}
        manifest_cache: dict[Path, tuple[tuple[int, ...], ExperimentManifest]] = {}
        self._contained_path(self.root)
        with os.scandir(self.root) as children:
            for entry in children:
                if entry.name.startswith((".", "_")):
                    continue
                child = self.root / entry.name
                try:
                    directory_stat = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if _is_link(directory_stat):
                    self._contained_path(child)
                    raise ExperimentRegistryError(
                        f"Linked experiment directory is not allowed: {child}"
                    )
                if not stat.S_ISDIR(directory_stat.st_mode):
                    continue
                directory_stamp = _stat_stamp(directory_stat)
                if self._directory_cache.get(child) != directory_stamp:
                    if self._contained_path(child) != child:
                        raise ExperimentRegistryError(
                            f"Linked experiment directory is not allowed: {child}"
                        )
                directory_cache[child] = directory_stamp
                manifest_path = child / MANIFEST_FILENAME
                try:
                    manifest_stat = manifest_path.lstat()
                except FileNotFoundError:
                    continue
                if _is_link(manifest_stat):
                    self._contained_path(manifest_path)
                    raise ExperimentRegistryError(
                        f"Linked experiment manifest is not allowed: {manifest_path}"
                    )
                if not stat.S_ISREG(manifest_stat.st_mode):
                    continue
                stamp = _stat_stamp(manifest_stat)
                cached = self._manifest_cache.get(manifest_path)
                if cached is not None and cached[0] == stamp:
                    manifest = cached[1]
                else:
                    self._contained_path(manifest_path)
                    expected = entry.name if RUN_ID_PATTERN.fullmatch(entry.name) else None
                    manifest = self._read_manifest(manifest_path, expected_run_id=expected)
                    self._validate_run_id(manifest.run_id)
                manifest_cache[manifest_path] = (stamp, manifest)
                if manifest.run_id in seen:
                    raise ExperimentRegistryError(
                        f"Duplicate experiment run_id in different directories: {manifest.run_id}"
                    )
                seen.add(manifest.run_id)
                entries.append((child, manifest))
        # Drop removed/renamed entries. Commit only a fully validated scan.
        self._directory_cache = directory_cache
        self._manifest_cache = manifest_cache
        return entries

    def _resolve_existing_run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        for path, manifest in self._entries():
            if manifest.run_id == run_id:
                self._contained_path(path)
                return path
        raise ExperimentNotFoundError(f"Experiment not found: {run_id}")

    def _manifest_path(self, run_id: str) -> Path:
        return self._resolve_existing_run_dir(run_id) / MANIFEST_FILENAME

    def _write_manifest(self, manifest: ExperimentManifest) -> None:
        manifest_path = self._manifest_path(manifest.run_id)
        _atomic_write_text(
            manifest_path,
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

    def _read_manifest(self, path: Path, *, expected_run_id: str | None) -> ExperimentManifest:
        read_error: OSError | json.JSONDecodeError | None = None
        data: Any = None
        for attempt in range(MANIFEST_READ_ATTEMPTS):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                read_error = None
                break
            except (OSError, json.JSONDecodeError) as exc:
                read_error = exc
                if attempt + 1 < MANIFEST_READ_ATTEMPTS:
                    time.sleep(MANIFEST_READ_RETRY_SECONDS * (attempt + 1))
        if read_error is not None:
            raise ExperimentRegistryError(
                f"Could not read experiment manifest: {path}"
            ) from read_error
        if not isinstance(data, dict):
            raise ExperimentRegistryError(f"Experiment manifest must be an object: {path}")
        try:
            manifest = ExperimentManifest.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise ExperimentRegistryError(f"Invalid experiment manifest: {path}") from exc
        if expected_run_id is not None and manifest.run_id != expected_run_id:
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


def _stat_stamp(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _is_link(value: os.stat_result) -> bool:
    # Windows junctions are reparse points but need not have symlink mode.
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
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
    return normalized


def _directory_name(current: datetime, label: str | None) -> str:
    # Keep the original label in experiment.json; sanitize only its directory name.
    safe_label = INVALID_LABEL_CHARACTERS.sub("_", label or "Unnamed")
    safe_label = safe_label.strip(" .") or "Unnamed"
    # Linux limits each path component to 255 bytes. Preserve complete Unicode
    # characters and leave room for the timestamp and a collision suffix.
    safe_label = safe_label.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    timestamp = current.strftime("%Y%m%dT%H%M%S_%f")[:19] + "Z"
    return f"{timestamp}_{safe_label}"


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
