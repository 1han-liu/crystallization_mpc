"""Version-1, per-event audit journal. Each service owns its own file.

The Controller journal is the authority for accepted/rejected outcomes; Central
also records submitted/unconfirmed requests. Writes use atomic replacement.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4
from crystallization_mpc.infra.file_version import file_version


SEED_EVENT_TYPE = "controller.add_seed"


class RuntimeHistoryEventError(ValueError):
    """A safe, event-specific diagnostic for a malformed journal entry."""


def runtime_event_type(entry):
    """Classify old and new journals without inventing configuration changes."""
    command = entry.get("command") or {}
    identity = f"{command.get('run_id', '?')}/{command.get('event_id', '?')}"
    if "changes" in command:
        if not isinstance(command["changes"], dict):
            raise RuntimeHistoryEventError(f"Runtime history event {identity}: changes must be an object.")
        kind = ("experiment.parameters.update" if "experiment_started_at" in command
                else "controller.runtime.update")
    elif (command.get("run_id") and command.get("event_id")
          and isinstance(command.get("added_at"), str) and command["added_at"]
          and "expected_revision" not in command):
        kind = SEED_EVENT_TYPE
    else:
        raise RuntimeHistoryEventError(f"Runtime history event {identity}: configuration changes are missing.")
    if entry.get("event_type", kind) != kind:
        raise RuntimeHistoryEventError(f"Runtime history event {identity}: event type does not match its command.")
    return kind


class RuntimeHistory:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self._queries = OrderedDict()
        self._loaded_version = None

    def _cached(self, key):
        cached = self._queries.get(key)
        if cached is not None and cached[0] == file_version(self.path):
            self._queries.move_to_end(key)
            return True, copy.deepcopy(cached[1])
        return False, None

    def _remember(self, key, value):
        self._queries[key] = (self._loaded_version, copy.deepcopy(value))
        self._queries.move_to_end(key)
        while len(self._queries) > 64:
            self._queries.popitem(last=False)
        return copy.deepcopy(value)

    def _load(self):
        for _ in range(3):
            version = file_version(self.path)
            if version is None:
                self._loaded_version = None
                return {"schema_version": 1, "entries": []}
            data = json.loads(self.path.read_text())
            if version != file_version(self.path):
                continue
            if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("entries"), list):
                raise ValueError("Invalid runtime history journal.")
            self._loaded_version = version
            return data
        raise ValueError("Runtime history changed while reading; retry the operation.")

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x") as stream:
                json.dump(data, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            # Windows has no O_DIRECTORY; file fsync and atomic replacement
            # still apply. Preserve directory durability where supported.
            directory_flag = getattr(os, "O_DIRECTORY", None)
            if directory_flag is not None:
                directory = os.open(self.path.parent, os.O_RDONLY | directory_flag)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
            self._queries.clear()

    @staticmethod
    def _merge(previous, entry):
        if previous["command"] != entry["command"]:
            raise ValueError("Runtime history event identity reused.")
        result, old_result = entry.get("result"), previous.get("result")
        if (old_result and not result) or ((old_result or {}).get("status") in {"applied", "rejected"}
                                          and (result or {}).get("status") == "pending"):
            return previous
        merged = {**previous, **entry, "sequence": previous["sequence"]}
        if (result and result.get("status") == "applied" and not result.get("no_change")
                and previous.get("grafana_sync") == "not_applicable"
                and runtime_event_type(merged) != SEED_EVENT_TYPE):
            merged["grafana_sync"] = "pending"
        return merged

    def save(self, entry):
        entry = copy.deepcopy(entry)
        entry["event_type"] = runtime_event_type(entry)
        command = entry["command"]
        run_id, event_id = command["run_id"], command["event_id"]
        result = entry.get("result")
        if result and (result.get("run_id") != run_id or result.get("event_id") != event_id):
            raise ValueError("Runtime history result identity mismatch.")
        with self.lock:
            key = ("event", run_id, event_id)
            found, previous = self._cached(key)
            if found and previous is not None and self._merge(previous, entry) == previous:
                return
            data = self._load()
            entries = data["entries"]
            previous = next((item for item in entries if item["command"]["run_id"] == run_id
                             and item["command"]["event_id"] == event_id), None)
            if previous is not None:
                merged = self._merge(previous, entry)
                if merged == previous:
                    self._remember(key, previous)
                    return
                entries[entries.index(previous)] = merged
            else:
                entry["sequence"] = max((item["sequence"] for item in entries), default=0) + 1
                entry.setdefault("grafana_sync", "pending" if entry["event_type"] != SEED_EVENT_TYPE
                                 and result and result.get("status") == "applied"
                                 and not result.get("no_change") else "not_applicable")
                entries.append(entry)
            self._write(data)

    def page(self, run_id: str, *, before: int | None = None, after: int | None = None, limit=50):
        if not run_id or not 1 <= limit <= 100 or (before is not None and after is not None):
            raise ValueError("Invalid history page request.")
        with self.lock:
            entries = [item for item in self._load()["entries"] if item["command"]["run_id"] == run_id]
            if before is not None:
                entries = [item for item in entries if item["sequence"] < before]
            if after is not None:
                entries = [item for item in entries if item["sequence"] > after]
            entries.sort(key=lambda item: item["sequence"], reverse=after is None)
            page = entries[:limit]
            return copy.deepcopy({"entries": page, "next_cursor": page[-1]["sequence"]
                                  if len(entries) > limit else None})

    def get(self, run_id: str, event_id: str):
        """Find a confirmed request independently of the recent-event window."""
        with self.lock:
            key = ("event", run_id, event_id)
            found, entry = self._cached(key)
            if found:
                return entry
            return self._remember(key, next((item for item in self._load()["entries"]
                                       if item["command"]["run_id"] == run_id
                                       and item["command"]["event_id"] == event_id), None))

    def pending_exports(self):
        with self.lock:
            found, entries = self._cached(("exports",))
            if found:
                return entries
            entries = [item for item in self._load()["entries"]
                                  if (item.get("result") or {}).get("status") == "applied"
                                  and not item["result"].get("no_change")
                                  and runtime_event_type(item) != SEED_EVENT_TYPE
                                  and item.get("grafana_sync") not in {"synced", "time_unavailable"}]
            return self._remember(("exports",), entries) if len(entries) <= 64 else copy.deepcopy(entries)

    def latest_fields(self, run_id):
        with self.lock:
            key = ("fields", run_id)
            found, entries = self._cached(key)
            if found:
                return entries
            entries = sorted((item for item in self._load()["entries"] if item["command"]["run_id"] == run_id),
                             key=lambda item: item["sequence"], reverse=True)
            selected, seen = [], set()
            for entry in entries:
                if runtime_event_type(entry) == SEED_EVENT_TYPE:
                    continue
                keys = set(entry["command"]["changes"])
                if keys - seen:
                    selected.append(entry)
                    seen.update(keys)
            return self._remember(key, selected)

    def mark_export(self, entry, error=None):
        self.save({**entry, "grafana_sync": "failed" if error else "synced", "grafana_error": error})
