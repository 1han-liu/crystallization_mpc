"""System-state paths and an explicit, offline migration of the legacy layout.

Run ``python -m crystallization_mpc.infra.storage_paths --root PATH`` to preview.
Stop all services before repeating the command with ``--apply``. Experiment
directories and explicitly configured paths are never moved by service startup.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

SYSTEM_DIRECTORY_NAME = "_system"
SYSTEM_STATE_FILENAMES = (
    ".central_experiment_state.json",
    ".central_run_configuration.json",
    ".central_runtime_history.json",
    ".central_runtime_requests.json",
    ".controller_runtime_history.json",
    ".controller_runtime_state.json",
    ".gsensor_experiment_state.json",
)


class StorageMigrationRequired(RuntimeError):
    """Legacy state must be migrated while the application services are stopped."""


class StorageMigrationConflict(RuntimeError):
    """Migration would overwrite different state or affect an active experiment."""


def _contained(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise StorageMigrationConflict(f"Storage path escapes the experiment root: {path}")
    if path.is_symlink():
        raise StorageMigrationConflict(f"Linked system-state paths cannot be migrated: {path}")
    return resolved


def _legacy_path(root: Path, filename: str) -> Path:
    if filename in {"params_runtime.yaml", ".params_runtime.yaml.lock"} or filename.startswith(
        "params_runtime.yaml.pre-upgrade-"
    ):
        return root / ".central" / filename
    return root / filename


def system_path(
    root: str | Path,
    filename: str,
    *,
    explicit_path: str | Path | None = None,
) -> Path:
    """Return a default path under _system, without moving existing user data.

    Explicit settings retain their original semantics, including legacy paths.
    Missing default state is safe on a new installation; legacy default state
    instead raises an actionable error so startup cannot silently reset it.
    """

    if Path(filename).name != filename or filename in {"", ".", ".."} or "\\" in filename:
        raise ValueError("System-state filename must be a single filename.")
    resolved_root = Path(root).expanduser().resolve(strict=False)
    target = resolved_root / SYSTEM_DIRECTORY_NAME / filename
    if explicit_path is not None:
        configured = Path(explicit_path).expanduser()
        if configured.resolve(strict=False) != target.resolve(strict=False):
            return configured
        # Compose spells out the default path. It still needs the legacy-state
        # guard; a genuinely custom path (including the old path) is respected.
    _contained(resolved_root, target.parent)
    _contained(resolved_root, target)
    legacy = _legacy_path(resolved_root, filename)
    if legacy.exists():
        raise StorageMigrationRequired(
            f"Legacy system state exists at {legacy}. End active experiments and stop all "
            "application services, then preview and apply the storage migration: "
            f'python -m crystallization_mpc.infra.storage_paths --root "{resolved_root}" --apply'
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _publish_once(path: Path, data: bytes) -> None:
    """Publish a complete file atomically without overwriting an existing file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.migration.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise StorageMigrationConflict(f"Existing migration file differs: {path}")
    finally:
        temporary.unlink(missing_ok=True)


def migrate_system_storage(root: str | Path, *, apply: bool = False) -> dict[str, Any]:
    """Preview or migrate known files, retaining a backup and rejecting conflicts.

    This function does not stop services. The caller must stop them before apply;
    active experiment manifests are an additional guard, not a process lock.
    """

    from crystallization_mpc.experiments import ExperimentRegistry

    resolved_root = Path(root).expanduser().resolve(strict=False)
    if not resolved_root.is_dir():
        raise FileNotFoundError(f"Experiment root does not exist: {resolved_root}")
    target_root = resolved_root / SYSTEM_DIRECTORY_NAME
    _contained(resolved_root, target_root)
    legacy_parameters = resolved_root / ".central"
    _contained(resolved_root, legacy_parameters)
    filenames = list(SYSTEM_STATE_FILENAMES)
    filenames.extend(("params_runtime.yaml", ".params_runtime.yaml.lock"))
    if legacy_parameters.is_dir():
        filenames.extend(
            item.name for item in legacy_parameters.glob("params_runtime.yaml.pre-upgrade-*.bak")
        )

    actions: list[dict[str, str]] = []
    conflicts: list[str] = []
    contents: dict[str, bytes] = {}
    for filename in filenames:
        source = _legacy_path(resolved_root, filename)
        destination = target_root / filename
        if not source.exists():
            continue
        backup = target_root / "migration_backup" / source.relative_to(resolved_root)
        for path in (source, destination, backup, backup.parent):
            _contained(resolved_root, path)
        if not source.is_file():
            conflicts.append(f"Legacy state is not a file: {source}")
            continue
        data = source.read_bytes()
        for existing in (destination, backup):
            if existing.exists() and (not existing.is_file() or existing.read_bytes() != data):
                conflicts.append(f"Existing file differs; no file will be overwritten: {existing}")
        actions.append({"source": str(source), "destination": str(destination), "backup": str(backup)})
        contents[str(source)] = data

    active = [
        item.run_id
        for item in ExperimentRegistry(resolved_root).list()
        if item.status.value not in {"created", "completed", "error"}
    ]
    if active:
        conflicts.append("End active experiments before migration: " + ", ".join(active))
    report: dict[str, Any] = {
        "root": str(resolved_root),
        "mode": "apply" if apply else "preview",
        "actions": actions,
        "conflicts": conflicts,
        "moved": 0,
        "requires_stopped_services": True,
    }
    if not apply:
        return report
    if conflicts:
        raise StorageMigrationConflict("\n".join(conflicts))
    for action in actions:
        source = Path(action["source"])
        data = contents[str(source)]
        if source.read_bytes() != data:
            raise StorageMigrationConflict(f"Legacy state changed during migration: {source}")
        _publish_once(Path(action["backup"]), data)
        _publish_once(Path(action["destination"]), data)
        if source.read_bytes() != data:
            raise StorageMigrationConflict(f"Legacy state changed during migration: {source}")
        source.unlink()
        report["moved"] += 1
    # Leave unrelated files in .central untouched.
    if legacy_parameters.is_dir() and not any(legacy_parameters.iterdir()):
        legacy_parameters.rmdir()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or migrate experiment system state into _system.")
    parser.add_argument("--root", required=True, help="Experiment storage root.")
    parser.add_argument("--apply", action="store_true", help="Apply after ending experiments and stopping all services.")
    args = parser.parse_args()
    try:
        report = migrate_system_storage(args.root, apply=args.apply)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Storage migration failed: {exc}\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 2 if report["conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
