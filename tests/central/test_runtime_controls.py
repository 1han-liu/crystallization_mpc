"""HTTP + real algorithm integration with an in-process transport, not RabbitMQ."""

from __future__ import annotations

import importlib
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from crystallization_mpc.apps.controller.algorithm.integration import CrystallizationControllerAdapter
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.apps.controller.service import ControllerService
from crystallization_mpc.experiments import InvalidExperimentStateError
from crystallization_mpc.messaging.controller_runtime import ControllerRuntimeUpdatePayload


class Transport:
    def __init__(self, controller):
        self.controller = controller
        self.sent = []
        self.fail = False
        self.deliver = True

    def publish_controller_runtime_command(self, event):
        self.sent.append(event.to_dict())
        if self.fail:
            raise ConnectionError("test disconnected broker")
        if self.deliver:
            return self.controller.on_message({
                "src": "central", "dst": "controller", "msg_type": "command",
                "name": "controller.runtime.update", "payload": event.to_dict(),
            })


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPERIMENT_ROOT", str(tmp_path / "central"))
    monkeypatch.setenv("PARAMS_FILE", str(tmp_path / "startup.yaml"))
    monkeypatch.setenv("RUN_CONFIGURATION_FILE", str(tmp_path / "startup.json"))
    app = importlib.import_module("crystallization_mpc.apps.central.ui.app")
    settings = ControllerSettings(
        rabbit_url="amqp://unused/", rabbit_exchange="test", rabbit_queue="test",
        adapter_spec=None, opcua_enabled=False, opcua_endpoint=None,
        influx_enabled=False, influx_url="http://unused", influx_org="test", influx_bucket="test",
    )
    controller = ControllerService(settings, adapter=CrystallizationControllerAdapter())
    transport = Transport(controller)
    central = app.CentralService(publisher=transport)
    run = central.experiments.create(label="isolated runtime test")
    run_id = run["run_id"]
    params = {"run_type": "simulation", "growth_rate_source": "simulated"}
    central.experiments.start(run_id, params_snapshot={"version": 1, "controller": params},
                              parameter_version=1)
    controller._apply_parameters({"version": 1, "params": params})
    controller._start_experiment({"run_id": run_id, "parameter_version": 1, "started_at": "now"})
    controller._control_tick_once(now=100)
    monkeypatch.setattr(central, "controller_status", lambda: {"available": True, **controller.status()})
    monkeypatch.setattr(app, "service", central)
    client = TestClient(app.web_app)  # No lifespan: no background consumers/equipment.
    yield app, central, controller, transport, client
    client.close()


def event(central, **changes):
    return {
        "run_id": central.experiments.current_run_id(), "event_id": "event-1",
        "expected_revision": 0, "changes": changes or {"control_target": "G"},
        "requested_at": "2026-09-10T12:00:00Z",
    }


def test_http_delivery_is_not_claimed_as_application(platform):
    _, central, controller, transport, client = platform
    original_defaults = central.run_configuration.to_dict()
    reply = client.post("/api/operation/controller/runtime", json=event(central)).json()
    assert reply["runtime_request"]["status"] == "pending"
    assert controller.runtime_revision == 1
    status = central.system_status()
    assert status["runtime_request"]["status"] == "applied"
    assert status["runtime_request"]["result"]["effective_tick"] == 2
    assert status["controller"]["runtime_controls"]["configuration"]["control_target"] == "G"
    assert central.run_configuration.to_dict() == original_defaults
    assert not central.params_path.exists()
    assert not central.run_configuration_store.path.exists()


@pytest.mark.parametrize("failure", ["history_write", "request_write", "history_read", "request_read"])
def test_status_survives_central_storage_failure_and_recovers(platform, monkeypatch, failure):
    _, central, controller, _, client = platform
    client.post("/api/operation/controller/runtime", json=event(central))
    store = central.runtime_request_store
    def broken(*args, **kwargs):
        raise OSError("test storage failure; do not leak connection credentials")
    with monkeypatch.context() as patcher:
        if failure == "history_write":
            patcher.setattr(store.history, "_write", broken)
        elif failure == "request_write":
            # Confirm the history write succeeded but the latest-request write did not.
            def fail_latest(run_id, entry):
                store.history.save(entry)
                broken()
            patcher.setattr(store, "save", fail_latest)
        elif failure == "history_read":
            patcher.setattr(store.history, "_load", broken)
        else:
            patcher.setattr(store, "load", broken)
        response = client.get("/api/system/status")
        assert response.status_code == 200
        status = response.json()
        assert status["controller"]["available"]
        assert status["controller"]["runtime_controls"]["revision"] == 1
        assert "OSError" in status["runtime_history_error"]
        assert "credentials" not in status["runtime_history_error"]
        if failure != "request_read":
            assert status["runtime_request"]["status"] == "applied"
            assert status["runtime_request"]["persistence_error"]
        else:
            assert status["runtime_request"] is None
        controller._control_tick_once(now=105)
        assert controller.control_tick_count == 2
    recovered = client.get("/api/system/status").json()
    assert recovered["runtime_history_error"] is None
    assert recovered["runtime_request"]["status"] == "applied"
    assert "persistence_error" not in recovered["runtime_request"]


def test_full_history_acknowledgment_unlocks_after_recent_window_expires(platform):
    _, central, controller, transport, client = platform
    client.post("/api/operation/controller/runtime", json=event(central))
    store = central.runtime_request_store
    run_id = central.experiments.current_run_id()
    pending = store.get(run_id)
    pending["created_at"] = time.time() - 60
    store.save(run_id, pending)
    result = controller.runtime_last_result
    store.history.save({**pending, "status": "applied", "result": result})
    # Further updates from another producer evict the unobserved ack from status.
    for i in range(51):
        payload = {**event(central, G_set=(i + 1) * 1e-8),
                   "event_id": f"external-{i}", "expected_revision": i + 1}
        transport.publish_controller_runtime_command(ControllerRuntimeUpdatePayload.from_mapping(payload))
    assert all(e["event_id"] != "event-1" for e in controller.status()["runtime_controls"]["recent_events"])
    assert client.get("/api/system/status").json()["runtime_request"]["status"] == "applied"
    next_event = {**event(central, G_set=7e-8), "event_id": "next", "expected_revision": 52}
    assert client.post("/api/operation/controller/runtime", json=next_event).status_code == 200


@pytest.mark.parametrize("journal", ["history", "requests"])
@pytest.mark.parametrize("content", ["not JSON", "[]"])
def test_corrupt_central_journal_does_not_hide_controller_or_overwrite_file(platform, journal, content):
    _, central, _, _, client = platform
    client.post("/api/operation/controller/runtime", json=event(central))
    store = central.runtime_request_store
    path = store.history.path if journal == "history" else store.path
    path.write_text(content)
    response = client.get("/api/system/status")
    assert response.status_code == 200
    status = response.json()
    assert status["controller"]["available"]
    assert status["runtime_history_error"]
    assert path.read_text() == content
    if journal == "history":
        history_response = client.get("/api/operation/controller/runtime/history", params={"run_id": central.experiments.current_run_id()})
        assert history_response.status_code == 200
        assert history_response.json()["error"]
        assert history_response.json()["entries"] == []


def test_revision_rejection_is_durable_and_not_a_controller_application(platform):
    _, central, controller, transport, client = platform
    payload = event(central)
    payload["expected_revision"] = 99
    reply = client.post("/api/operation/controller/runtime", json=payload)
    assert reply.status_code == 409
    history = client.get("/api/operation/controller/runtime/history", params={"run_id": payload["run_id"]}).json()
    item = history["entries"][0]
    assert item["status"] == "rejected"
    assert item["result"]["source"] == "central"
    assert "applied_at" not in item["result"]
    assert not transport.sent and controller.runtime_revision == 0


def test_controller_history_sync_fetches_more_than_one_hundred_and_deduplicates(platform, monkeypatch):
    app, central, _, _, _ = platform
    run_id = central.experiments.current_run_id()
    items = []
    for i in range(1, 126):
        payload = event(central, G_set=i * 1e-8)
        payload.update(event_id=f"history-{i}", expected_revision=i - 1)
        items.append({"sequence": i, "command": payload, "status": "applied",
                      "result": {"run_id": run_id, "event_id": payload["event_id"],
                                 "status": "applied", "revision": i}})
    cursors = []

    def response(url, **kwargs):
        cursor = int(app.urllib.parse.parse_qs(app.urllib.parse.urlparse(url).query)["after"][0])
        cursors.append(cursor)
        remaining = [item for item in items if item["sequence"] > cursor]
        page = {"entries": remaining[:100], "next_cursor": remaining[99]["sequence"] if len(remaining) > 100 else None}
        return io.StringIO(json.dumps(page))

    monkeypatch.setattr(app.urllib.request, "urlopen", response)
    status = {"available": True, "runtime_controls": {"history_supported": True}}
    central._sync_runtime_history(run_id, status, all_pages=True)
    central._sync_runtime_history(run_id, status, all_pages=True)
    assert cursors == [0, 100, 0, 100]
    first = central.runtime_request_store.history.page(run_id, limit=100)
    second = central.runtime_request_store.history.page(run_id, before=first["next_cursor"], limit=100)
    assert len(first["entries"]) + len(second["entries"]) == 125


def test_same_event_retry_after_lost_http_response_is_not_resent(platform):
    _, central, controller, transport, client = platform
    payload = event(central)
    assert client.post("/api/operation/controller/runtime", json=payload).status_code == 200
    reply = client.post("/api/operation/controller/runtime", json=payload).json()
    assert reply["requested"] is False
    assert reply["runtime_request"]["status"] == "applied"
    assert len(transport.sent) == 1
    assert controller.runtime_revision == 1


def test_disconnected_delivery_retry_keeps_event_and_blocks_other_changes(platform):
    _, central, controller, transport, client = platform
    transport.fail = True
    payload = event(central)
    response = client.post("/api/operation/controller/runtime", json=payload)
    assert response.status_code == 200
    assert response.json()["runtime_request"]["transport_error"]
    assert controller.runtime_revision == 0
    other = {**payload, "event_id": "other"}
    assert client.post("/api/operation/controller/runtime", json=other).status_code == 409
    transport.fail = False
    assert client.post("/api/operation/controller/runtime", json=payload).status_code == 200
    assert transport.sent[0] == transport.sent[1]
    assert central.system_status()["runtime_request"]["status"] == "applied"


def test_timeout_is_unconfirmed_and_survives_central_restart(platform, monkeypatch):
    app, central, controller, transport, client = platform
    transport.deliver = False
    payload = event(central)
    client.post("/api/operation/controller/runtime", json=payload)
    run_id = payload["run_id"]
    stored = central.runtime_request_store.get(run_id)
    stored["created_at"] = time.time() - 31
    central.runtime_request_store.save(run_id, stored)
    recovered = app.CentralService(publisher=transport)
    monkeypatch.setattr(recovered, "controller_status", central.controller_status)
    assert recovered.system_status()["runtime_request"]["status"] == "timeout"
    assert controller.runtime_revision == 0
    transport.deliver = True
    recovered.update_controller_runtime(app.ControllerRuntimeUpdatePayload.from_mapping(payload))
    assert recovered.system_status()["runtime_request"]["status"] == "applied"


@pytest.mark.parametrize("changes", [
    {"dt": 10}, {"control_target": "sigma+G"}, {"G_set": 0}, {"sigma_set": "0.035"},
    {"sigma_set": True}, {"adaptation_mode": ""}, {"adaptation_enabled": "true"},
])
def test_invalid_http_request_cannot_reach_controller(platform, changes):
    _, central, controller, transport, client = platform
    response = client.post("/api/operation/controller/runtime", json=event(central, **changes))
    assert response.status_code == 422
    assert transport.sent == []
    assert controller.runtime_revision == 0


def test_other_run_and_old_revision_rejected(platform):
    _, central, controller, transport, client = platform
    payload = event(central)
    assert client.post("/api/operation/controller/runtime",
                       json={**payload, "run_id": "other"}).status_code == 409
    assert client.post("/api/operation/controller/runtime",
                       json={**payload, "expected_revision": 1}).status_code == 409
    assert transport.sent == []


def test_running_full_configuration_is_still_locked(platform):
    _, central, controller, transport, client = platform
    with pytest.raises(InvalidExperimentStateError, match="locked"):
        central.update_run_configuration(central.run_configuration)
    assert controller.runtime_revision == 0


def test_legacy_http_adaptation_preserves_actual_selected_mode(platform):
    _, central, controller, transport, client = platform
    client.post("/api/operation/controller/runtime", json=event(central, adaptation_mode="all"))
    central.system_status()
    response = client.post("/api/operation/controller/adaptation", json={
        "expected_run_id": central.experiments.current_run_id(), "enabled": True,
    })
    assert response.status_code == 200
    assert response.json()["event"]["event_id"]
    assert controller.adapter.controller.adaptation_mode == "all"
    assert controller.adapter.controller.adaptation_enabled is True
    assert central.run_configuration.adaptation_mode == "E_A"
