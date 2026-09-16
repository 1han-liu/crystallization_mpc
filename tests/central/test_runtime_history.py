from __future__ import annotations

import copy
from datetime import datetime

import pytest

from crystallization_mpc.infra.runtime_history import RuntimeHistory
from crystallization_mpc.apps.central.runtime_controls import RuntimeRequestStore
from crystallization_mpc.apps.central.runtime_telemetry import export_pending, write_runtime_event


def entry(index=1, run="run-a", status="applied"):
    command = {"run_id": run, "event_id": f"e-{index}", "expected_revision": index - 1,
               "changes": {"G_set": index * 1e-8}, "requested_at": "2026-09-10T12:00:00Z"}
    result = {"run_id": run, "event_id": command["event_id"], "status": status,
              "requested_at": command["requested_at"], "processed_at": "2026-09-10T12:00:10Z",
              "applied_at": "2026-09-10T12:00:11Z", "effective_tick": index + 1,
              "revision": index, "before": {"G_set": 0}, "configuration": {"G_set": index * 1e-8},
              "no_change": False}
    return {"command": command, "status": status, "result": result if status in {"applied", "rejected"} else None,
            "created_at": 0}


def test_pagination_recovery_and_isolation(tmp_path):
    history = RuntimeHistory(tmp_path / "events.json")
    for i in range(1, 126):
        history.save(entry(i))
    history.save(entry(999, "other"))
    recovered = RuntimeHistory(history.path)
    cursor, ids = None, []
    while True:
        page = recovered.page("run-a", before=cursor, limit=30)
        ids.extend(item["command"]["event_id"] for item in page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert ids == [f"e-{i}" for i in range(125, 0, -1)]
    assert len(recovered.page("run-a", after=100, limit=100)["entries"]) == 25


def test_pending_timeout_retry_merge_and_export_status(tmp_path):
    history = RuntimeHistory(tmp_path / "events.json")
    history.save(entry(status="pending"))
    history.save(entry(status="timeout"))
    history.save(entry())
    history.save(entry(status="pending"))
    items = history.page("run-a")["entries"]
    assert len(items) == 1 and items[0]["status"] == "applied"
    assert items[0]["grafana_sync"] == "pending"
    history.mark_export(items[0])
    history.save(entry())
    assert history.page("run-a")["entries"][0]["grafana_sync"] == "synced"
    wrong = entry()
    wrong["command"]["changes"] = {"G_set": 99}
    with pytest.raises(ValueError, match="reused"):
        history.save(wrong)


def test_result_identity_and_legacy_latest_import(tmp_path):
    store = RuntimeRequestStore(tmp_path)
    store.save("run-a", entry())
    assert store.history.page("run-a")["entries"][0]["command"]["event_id"] == "e-1"
    wrong = entry()
    wrong["result"]["run_id"] = "other"
    with pytest.raises(ValueError, match="identity"):
        store.history.save(wrong)


def test_field_feedback_older_than_latest_fifty(tmp_path):
    history = RuntimeHistory(tmp_path / "events.json")
    first = entry()
    first["command"]["changes"] = {"sigma_set": 0.035}
    history.save(first)
    for i in range(2, 65):
        history.save(entry(i))
    assert [item["command"]["event_id"] for item in history.latest_fields("run-a")] == ["e-64", "e-1"]


class Writer:
    def __init__(self):
        self.points = {}
        self.fail = False

    def write_tagged_fields(self, fields, *, tags, measurement, timestamp):
        if self.fail:
            raise OSError("secret connection must not be logged")
        self.points[(measurement, tuple(sorted(tags.items())), timestamp)] = fields


def test_event_time_idempotence_failure_retry_and_no_fabrication(tmp_path):
    history, writer = RuntimeHistory(tmp_path / "events.json"), Writer()
    history.save(entry())
    writer.fail = True
    export_pending(history, writer)
    failed = history.page("run-a")["entries"][0]
    assert failed["grafana_sync"] == "failed" and failed["grafana_error"] == "OSError"
    writer.fail = False
    export_pending(history, writer)
    write_runtime_event(writer, entry())  # Retry after write succeeded but cursor save failed.
    assert len(writer.points) == 1
    key = next(iter(writer.points))
    assert key[-1] == datetime.fromisoformat("2026-09-10T12:00:11+00:00")
    assert key[-1] != datetime.fromisoformat("2026-09-10T12:00:00+00:00")
    noop = entry(2)
    noop["result"]["no_change"] = True
    history.save(noop)
    history.save(entry(3, status="rejected"))
    legacy = entry(4)
    legacy["result"].pop("applied_at")
    history.save(legacy)
    history.save(entry(5))
    export_pending(history, writer)
    assert len(writer.points) == 2
    assert next(item for item in history.page("run-a")["entries"] if item["command"]["event_id"] == "e-4")["grafana_sync"] == "time_unavailable"
