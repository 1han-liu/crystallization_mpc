"""Durable per-run requests; never changes a startup parameter/default file."""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from uuid import uuid4
from typing import Any

from crystallization_mpc.messaging.controller_runtime import ControllerRuntimeUpdatePayload
from crystallization_mpc.infra.runtime_history import RuntimeHistory


class RuntimeRequestStore:
    """Used under CentralService's lock (single Central process)."""

    def __init__(self, root: Path):
        self.path = root / ".central_runtime_requests.json"
        self.history = RuntimeHistory(root / ".central_runtime_history.json")
        self.observation_error: str | None = None

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("Invalid Central runtime request journal.")
        runs = data.get("runs")
        if not isinstance(runs, dict):
            raise ValueError("Invalid Central runtime request runs.")
        for run_id, entry in runs.items():
            command = ControllerRuntimeUpdatePayload.from_mapping(entry["command"])
            if command.run_id != run_id:
                raise ValueError("Central runtime request run_id mismatch.")
        return runs

    def get(self, run_id: str | None) -> dict[str, Any] | None:
        return copy.deepcopy(self.load().get(run_id)) if run_id else None

    def save(self, run_id: str, entry: dict[str, Any]) -> None:
        self.history.save(entry)
        runs = self.load()
        runs[run_id] = copy.deepcopy(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "runs": runs}, stream, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def observe(self, run_id: str | None, controller: dict[str, Any]) -> dict[str, Any] | None:
        # Status observation must not take down the UI when storage is broken.
        # Command submission still uses strict save-before-delivery semantics.
        self.observation_error = None
        errors = []

        def storage_error(exc):
            errors.append(f"Central runtime persistence incomplete: {type(exc).__name__}")
            self.observation_error = "; ".join(dict.fromkeys(errors))

        try:
            entry = self.get(run_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            storage_error(exc)
            return None
        if entry is None:
            return None
        original = copy.deepcopy(entry)
        results = []
        try:
            archived = self.history.get(run_id, entry["command"]["event_id"])
            if archived and archived.get("result"):
                if archived["command"] != entry["command"]:
                    raise ValueError("Archived runtime command mismatch.")
                results.append(archived["result"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            storage_error(exc)
        if controller.get("available") and controller.get("current_run_id") == run_id:
            runtime = controller.get("runtime_controls") or {}
            results = list(runtime.get("recent_events") or []) + results
            if runtime.get("last_result"):
                results.insert(0, runtime["last_result"])
        for result in results:
            if (result.get("event_id") == entry["command"]["event_id"]
                    and result.get("run_id") == run_id
                    and result.get("status") in {"applied", "rejected"}):
                entry = {**entry, "status": result["status"], "result": copy.deepcopy(result)}
                break
        if entry["status"] not in {"applied", "rejected"} and time.time() - entry["created_at"] >= 30:
            entry["status"] = "timeout"
        try:
            if entry != original:
                self.save(run_id, entry)
            else:
                self.history.save(entry)  # Import legacy requests; retry failed history writes.
        except (OSError, ValueError, KeyError, TypeError) as exc:
            storage_error(exc)
        # This is a real Controller acknowledgment even if persisting it failed.
        # Do not write a transient storage warning into the durable command.
        if self.observation_error:
            entry = {**entry, "persistence_error": self.observation_error}
        return entry
