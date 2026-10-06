"""Version-1, per-event audit journal. Each service owns its own file.

The Controller journal is the authority for accepted/rejected outcomes; Central
also records submitted/unconfirmed requests. Writes use atomic replacement.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from uuid import uuid4


class RuntimeHistory:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def _load(self):
        if not self.path.exists():
            return {"schema_version": 1, "entries": []}
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("entries"), list):
            raise ValueError("Invalid runtime history journal.")
        return data

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

    def save(self, entry):
        entry = copy.deepcopy(entry)
        command = entry["command"]
        run_id, event_id = command["run_id"], command["event_id"]
        result = entry.get("result")
        if result and (result.get("run_id") != run_id or result.get("event_id") != event_id):
            raise ValueError("Runtime history result identity mismatch.")
        with self.lock:
            data = self._load()
            entries = data["entries"]
            previous = next((item for item in entries if item["command"]["run_id"] == run_id
                             and item["command"]["event_id"] == event_id), None)
            if previous is not None:
                if previous["command"] != command:
                    raise ValueError("Runtime history event identity reused.")
                if previous.get("result") and not result:
                    return  # A late pending response cannot erase a confirmed outcome.
                merged = {**previous, **entry, "sequence": previous["sequence"]}
                if result and result.get("status") == "applied" and not result.get("no_change") and previous.get("grafana_sync") == "not_applicable":
                    merged["grafana_sync"] = "pending"
                if merged == previous:
                    return
                entries[entries.index(previous)] = merged
            else:
                entry["sequence"] = max((item["sequence"] for item in entries), default=0) + 1
                entry.setdefault("grafana_sync", "pending" if result and result.get("status") == "applied"
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
            return copy.deepcopy(next((item for item in self._load()["entries"]
                                       if item["command"]["run_id"] == run_id
                                       and item["command"]["event_id"] == event_id), None))

    def pending_exports(self):
        with self.lock:
            return copy.deepcopy([item for item in self._load()["entries"]
                                  if (item.get("result") or {}).get("status") == "applied"
                                  and not item["result"].get("no_change")
                                  and item.get("grafana_sync") not in {"synced", "time_unavailable"}])

    def latest_fields(self, run_id):
        with self.lock:
            entries = sorted((item for item in self._load()["entries"] if item["command"]["run_id"] == run_id),
                             key=lambda item: item["sequence"], reverse=True)
            selected, seen = [], set()
            for entry in entries:
                keys = set(entry["command"]["changes"])
                if keys - seen:
                    selected.append(entry)
                    seen.update(keys)
            return copy.deepcopy(selected)

    def mark_export(self, entry, error=None):
        self.save({**entry, "grafana_sync": "failed" if error else "synced", "grafana_error": error})
