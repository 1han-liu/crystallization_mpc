"""Durable per-run requests; never changes a startup parameter/default file."""

from __future__ import annotations

import copy
import json
import os
import time
import threading
from functools import wraps
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4
from typing import Any

from crystallization_mpc.messaging.controller_runtime import ControllerRuntimeUpdatePayload
from crystallization_mpc.messaging.run_parameters import RunParametersUpdatePayload
from crystallization_mpc.infra.runtime_history import RuntimeHistory
from crystallization_mpc.infra.storage_paths import system_path
from crystallization_mpc.infra.file_version import file_version


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class RuntimeRequestStore:
    """Own request-journal coordination independently of Central UI state."""

    def __init__(self, root: Path):
        self._lock = threading.RLock()
        self.path = system_path(root, ".central_runtime_requests.json")
        self.history = RuntimeHistory(system_path(root, ".central_runtime_history.json"))
        self.observation_error: str | None = None
        self._entries = OrderedDict()
        self._loaded_version = None

    @_synchronized
    def load(self) -> dict[str, Any]:
        for _ in range(3):
            version = file_version(self.path)
            if version is None:
                self._loaded_version = None
                return {}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if version == file_version(self.path):
                self._loaded_version = version
                break
        else:
            raise ValueError("Runtime request journal changed while reading; retry the operation.")
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("Invalid Central runtime request journal.")
        runs = data.get("runs")
        if not isinstance(runs, dict):
            raise ValueError("Invalid Central runtime request runs.")
        for run_id, entry in runs.items():
            parser = (RunParametersUpdatePayload if "experiment_started_at" in entry["command"]
                      else ControllerRuntimeUpdatePayload)
            command = parser.from_mapping(entry["command"])
            if command.run_id != run_id:
                raise ValueError("Central runtime request run_id mismatch.")
        return runs

    @_synchronized
    def get(self, run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            return None
        cached = self._entries.get(run_id)
        if cached is not None and cached[0] == file_version(self.path):
            self._entries.move_to_end(run_id)
            return copy.deepcopy(cached[1])
        entry = self.load().get(run_id)
        self._entries[run_id] = (self._loaded_version, copy.deepcopy(entry))
        self._entries.move_to_end(run_id)
        while len(self._entries) > 32:
            self._entries.popitem(last=False)
        return entry

    @_synchronized
    def get_event(self, run_id, event_id):
        latest = self.get(run_id)
        return (latest if latest and latest["command"]["event_id"] == event_id
                else self.history.get(run_id, event_id))

    @_synchronized
    def save(self, run_id: str, entry: dict[str, Any]) -> None:
        previous = self._entries.get(run_id)
        unchanged = (previous is not None and previous[0] == file_version(self.path)
                     and previous[1] == entry)
        self.history.save(entry)
        if unchanged:
            return
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
            self._entries.clear()

    @_synchronized
    def update_delivery(self, run_id, event_id, delivery):
        entry = self.get(run_id)
        if entry is None or entry["command"]["event_id"] != event_id:
            return
        entry["delivery"] = delivery
        entry["transport_error"] = delivery.get("error")
        if delivery["status"] == "cancelled" and entry["status"] not in {"applied", "rejected"}:
            entry.update(status="rejected", result={"run_id": run_id, "event_id": event_id,
                         "status": "rejected", "source": "central", "reason": delivery["error"]})
        self.save(run_id, entry)

    @_synchronized
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
        archived = None
        try:
            archived = self.history.get(run_id, entry["command"]["event_id"])
            if archived and archived.get("result"):
                if archived["command"] != entry["command"]:
                    raise ValueError("Archived runtime command mismatch.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            archived = None
            storage_error(exc)
        entry = self.project(entry, controller, archived)
        try:
            if entry != original:
                self.save(run_id, entry)
            elif archived is None or any(archived.get(key) != value for key, value in entry.items()):
                self.history.save(entry)  # Import/repair only missing or changed records.
        except (OSError, ValueError, KeyError, TypeError) as exc:
            storage_error(exc)
        if self.observation_error:
            entry = {**entry, "persistence_error": self.observation_error}
        return entry

    @staticmethod
    def project(entry, controller, archived=None):
        """Resolve this exact event against an observation without writing storage."""
        entry = copy.deepcopy(entry)
        run_id = entry["command"]["run_id"]
        results = [archived["result"]] if archived and archived.get("result") else []
        if controller.get("available") and controller.get("current_run_id") == run_id:
            runtime = controller.get("runtime_controls") or {}
            results = list(runtime.get("recent_events") or []) + results
            if runtime.get("last_result"):
                results.insert(0, runtime["last_result"])
            parameters = controller.get("run_parameters") or {}
            results = list(parameters.get("events") or []) + results
            if parameters.get("last_result"):
                results.insert(0, parameters["last_result"])
        # An older cached pending observation cannot hide a durable outcome.
        results.sort(key=lambda result: result.get("status") not in {"applied", "rejected"})
        for result in results:
            if (result.get("event_id") == entry["command"]["event_id"]
                    and result.get("run_id") == run_id
                    and result.get("status") in {"pending", "applied", "rejected"}):
                if entry["status"] in {"applied", "rejected"} and result["status"] == "pending":
                    continue
                entry = {**entry, "status": result["status"], "result": copy.deepcopy(result)}
                break
        if entry["status"] not in {"applied", "rejected"} and time.time() - entry.get("created_at", time.time()) >= 30:
            entry["status"] = "timeout"
        return entry
