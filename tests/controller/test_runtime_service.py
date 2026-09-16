from __future__ import annotations

import copy
import json
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from crystallization_mpc.apps.controller.adapter import NoOpControllerAdapter
from crystallization_mpc.apps.controller.algorithm.integration import CrystallizationControllerAdapter
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.apps.controller.service import ControllerService, ControllerState
from crystallization_mpc.messaging.controller_runtime import ControllerRuntimeUpdatePayload


def settings(root=None):
    return ControllerSettings(
        rabbit_url="amqp://guest:guest@localhost/%2F", rabbit_exchange="runtime-test",
        rabbit_queue="runtime-test.controller", adapter_spec=None, opcua_enabled=False,
        opcua_endpoint=None, influx_enabled=False, influx_url="http://unused:8086",
        influx_org="test", influx_bucket="test", experiment_root=str(root) if root else None,
    )


def service_running(root=None, adapter=None):
    service = ControllerService(settings(root), adapter=adapter or CrystallizationControllerAdapter())
    service._apply_parameters({"version": 1, "params": {
        "run_type": "simulation", "growth_rate_source": "simulated",
    }})
    service._start_experiment({
        "run_id": "runtime-test", "parameter_version": 1, "started_at": "2026-09-10T12:00:00Z",
    })
    service._control_tick_once(now=100)
    return service


def command(changes=None, *, event_id="event-1", revision=0, run_id="runtime-test"):
    return {
        "src": "central", "dst": "controller", "msg_type": "command",
        "name": "controller.runtime.update",
        "payload": ControllerRuntimeUpdatePayload(
            run_id, event_id, revision, changes or {"control_target": "G"},
            "2026-09-10T12:01:00Z",
        ).to_dict(),
    }


def test_real_algorithm_command_ack_and_next_tick_telemetry():
    service = service_running()
    initial_parameters = copy.deepcopy(service.parameters)
    event = command()
    result = service.on_message(event)
    assert result["accepted"] and result["status"] == "applied"
    assert result["effective_tick"] == 2
    assert result["revision"] == 1
    assert service.control_tick_count == 1
    assert service.parameters == initial_parameters
    service._control_tick_once(now=105)
    output = service.last_control_output
    assert output["runtime_configuration"]["control_target"] == "G"
    assert output["runtime_revision"] == 1
    assert service.adapter.controller.params["target"] == "G"
    assert service.status()["runtime_controls"]["last_result"] == result


def test_durable_history_beyond_status_window_and_restart(tmp_path):
    service = service_running(tmp_path)
    for i in range(65):
        result = service.on_message(command({"G_set": (i + 1) * 1e-8}, event_id=f"history-{i}", revision=i))
        assert result["status"] == "applied"
        assert result["applied_at"] and result["requested_at"] != result["applied_at"]
    assert len(service.status()["runtime_controls"]["recent_events"]) == 50
    page = service.runtime_history_page("runtime-test", limit=100)
    assert len(page["entries"]) == 65
    restored = ControllerService(settings(tmp_path), adapter=CrystallizationControllerAdapter())
    assert len(restored.runtime_history_page("runtime-test", after=0, limit=100)["entries"]) == 65
    duplicate = restored.on_message(command({"G_set": 1e-8}, event_id="history-0", revision=0))
    assert duplicate["duplicate"]
    assert len(restored.runtime_history_page("runtime-test", limit=100)["entries"]) == 65
    assert not restored.runtime_history_page("other-run")["entries"]


def test_real_fit_timestamp_is_service_metadata_and_survives_restart(tmp_path):
    service = ControllerService(settings(tmp_path), adapter=CrystallizationControllerAdapter())
    service._apply_parameters({"version": 1, "params": {
        "run_type": "simulation", "growth_rate_source": "simulated", "c_init": 0.4,
    }})
    service._start_experiment({"run_id": "runtime-test", "parameter_version": 1,
                               "started_at": "2026-09-10T12:00:00Z"})
    service.on_message(command({"adaptation_enabled": True}))
    assert service.last_fit_success_at is None
    for i in range(1, 46):
        service._control_tick_once(now=i * 5)
    fitting = service.status()["adaptation"]["fitting"]
    assert fitting["fit_count"] > 0 and fitting["last_success_at"]
    assert "last_success_at" not in service.adapter.controller.export_state()["adaptation"]["diagnostics"]
    timestamp = service.last_fit_success_at
    service.on_message(command({"adaptation_enabled": False}, event_id="off", revision=1))
    service._control_tick_once(now=230)
    assert service.last_fit_success_at == timestamp
    restored = ControllerService(settings(tmp_path), adapter=CrystallizationControllerAdapter())
    assert restored.last_fit_success_at == timestamp


def test_history_disk_failure_is_visible_and_recovered_from_saved_state(tmp_path):
    service = service_running(tmp_path)
    with patch.object(service.runtime_history, "_write", side_effect=OSError("test disk outage")):
        result = service.on_message(command())
        assert result["status"] == "applied"
        assert service.status()["runtime_controls"]["history_error"]
        assert service.runtime_history_page("runtime-test")["error"]
        service._control_tick_once(now=105)
        assert service.control_tick_count == 2
    restored = ControllerService(settings(tmp_path), adapter=CrystallizationControllerAdapter())
    history = restored.runtime_history_page("runtime-test")
    assert history["error"] is None
    assert len(history["entries"]) == 1
    assert history["entries"][0]["result"]["applied_at"] == result["applied_at"]


def test_duplicate_is_idempotent_and_event_id_cannot_change_content():
    service = service_running()
    result = service.on_message(command())
    snapshot = service.adapter.export_state()
    duplicate = service.on_message(command())
    assert duplicate == {**result, "duplicate": True}
    assert service.runtime_revision == 1
    assert service.adapter.export_state() == snapshot
    changed = service.on_message(command({"control_target": "sigma"}))
    assert not changed["accepted"]
    assert "reused" in changed["reason"]
    assert service.runtime_revision == 1


def test_storage_failure_reports_active_but_not_saved_and_recovers(tmp_path):
    service = service_running(tmp_path)
    with patch("crystallization_mpc.apps.controller.service.os.replace", side_effect=OSError("disk unavailable")):
        result = service.on_message(command())
    assert result["status"] == "applied"  # Actual in-memory application is not rolled back.
    assert service.status()["recovery"]["status"] == "persistence_error"
    assert "disk unavailable" in service.status()["recovery"]["error"]
    assert service.runtime_revision == 1
    service._try_persist_state_locked()
    assert service.status()["recovery"]["status"] == "saved"
    assert service.status()["recovery"]["error"] is None
    recovered = ControllerService(settings(tmp_path), adapter=CrystallizationControllerAdapter())
    assert recovered.runtime_revision == 1
    assert recovered.runtime_configuration_current == service.runtime_configuration_current


@pytest.mark.parametrize("cause", ["stale", "wrong_run", "wrong_sender", "stopped", "invalid"])
def test_rejected_commands_do_not_change_config_or_stop_a_running_experiment(cause):
    service = service_running()
    msg = command()
    if cause == "stale":
        msg["payload"]["expected_revision"] = 1
    elif cause == "wrong_run":
        msg["payload"]["run_id"] = "other"
    elif cause == "wrong_sender":
        msg["src"] = "gsensor"
    elif cause == "stopped":
        service.state = ControllerState.STOPPED
    else:
        msg["payload"]["changes"]["G_set"] = -1
    before = service.adapter.export_state()
    result = service.on_message(msg)
    assert not result["accepted"]
    assert service.runtime_revision == 0
    assert service.adapter.export_state() == before
    if cause != "stopped":
        assert service.state == ControllerState.RUNNING


def test_noop_adapter_rejects_instead_of_claiming_success():
    service = service_running(adapter=NoOpControllerAdapter())
    result = service.on_message(command())
    assert not result["accepted"]
    assert "does not support" in result["reason"]
    assert service.state == ControllerState.RUNNING


def test_legacy_adaptation_command_uses_same_revision_and_actual_mode():
    service = service_running()
    event = {
        "src": "central", "dst": "controller", "msg_type": "command",
        "name": "controller.adaptation.set",
        "payload": {"run_id": "runtime-test", "event_id": "legacy", "enabled": False,
                    "mode": "all", "requested_at": "now"},
    }
    assert service.on_message(event)["accepted"]
    assert service.runtime_revision == 1
    assert service.adapter.controller.adaptation_mode == "all"
    assert service.adapter.controller.adaptation_enabled is False
    assert service.on_message(event)["duplicate"]
    assert service.runtime_revision == 1


def test_runtime_journal_and_algorithm_recover_together(tmp_path):
    service = service_running(tmp_path)
    assert service.on_message(command({"control_target": "G", "G_set": 4e-8,
                                       "adaptation_mode": "all"}))["accepted"]
    saved = json.loads(service.state_path.read_text())
    assert saved["runtime_controls"]["events"]["event-1"]["result"]["effective_tick"] == 2
    restored = ControllerService(settings(tmp_path), adapter=CrystallizationControllerAdapter())
    assert restored.recovery_status == "restored"
    assert restored.runtime_revision == 1
    assert restored.runtime_configuration_current == service.runtime_configuration_current
    assert restored.on_message(command({"control_target": "G", "G_set": 4e-8,
                                        "adaptation_mode": "all"}))["duplicate"]
    service._control_tick_once(now=105)
    restored._control_tick_once(now=105)
    assert restored.last_control_output["result"] == service.last_control_output["result"]
    assert restored.parameters == service.parameters


def test_old_archive_without_runtime_fields_is_still_recoverable(tmp_path):
    service = service_running(tmp_path)
    service.adapter.set_adaptation(False, "k_0")
    service.adaptation_mode = "k_0"
    document = service._state_document_locked()
    document.pop("runtime_controls")
    service.state_path.write_text(json.dumps(document))
    restored = ControllerService(settings(tmp_path), adapter=CrystallizationControllerAdapter())
    assert restored.recovery_status == "restored"
    assert restored.runtime_revision == 0
    assert restored.runtime_configuration_current["adaptation_mode"] == "k_0"
    assert restored.on_message(command())["accepted"]


def test_saved_runtime_must_agree_with_algorithm(tmp_path):
    service = service_running(tmp_path)
    document = service._state_document_locked()
    document["runtime_controls"]["configuration"]["control_target"] = "G"
    service.state_path.write_text(json.dumps(document))
    restored = ControllerService(settings(tmp_path), adapter=CrystallizationControllerAdapter())
    assert restored.recovery_status == "error"
    assert restored.state == ControllerState.ERROR
    assert "disagree" in restored.recovery_error


def test_new_run_resets_runtime_overlay_not_original_defaults():
    service = service_running()
    assert service.on_message(command())["accepted"]
    service._stop_experiment({"run_id": "runtime-test", "stopped_at": "now"})
    service._start_experiment({"run_id": "new-run", "parameter_version": 1, "started_at": "later"})
    assert service.runtime_revision == 0
    assert service.runtime_events == {}
    assert service.runtime_configuration_current["control_target"] == "sigma"
    assert not service.on_message(command())["accepted"]


def test_concurrent_old_revision_has_exactly_one_winner():
    service = service_running()
    barrier = Barrier(2)

    def send(event_id, value):
        barrier.wait(timeout=5)
        return service.on_message(command({"G_set": value}, event_id=event_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, "first", 4e-8)
        second = pool.submit(send, "second", 5e-8)
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert sum(result["accepted"] for result in results) == 1
    assert service.runtime_revision == 1
    assert len(service.runtime_events) == 2
    winner = next(result for result in results if result["accepted"])
    assert service.adapter.runtime_configuration() == winner["configuration"]


def test_update_waits_until_tick_finishes(monkeypatch):
    service = service_running()
    entered, release, updated = Event(), Event(), Event()
    step = service.adapter.step

    def blocked_step(tick):
        entered.set()
        assert release.wait(timeout=5)
        return step(tick)

    def send():
        result = service.on_message(command())
        updated.set()
        return result

    monkeypatch.setattr(service.adapter, "step", blocked_step)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tick_future = pool.submit(service._control_tick_once, now=105)
        assert entered.wait(timeout=5)
        command_future = pool.submit(send)
        try:
            assert not updated.wait(timeout=0.1)
        finally:
            release.set()
        assert tick_future.result(timeout=5)["output_valid"]
        result = command_future.result(timeout=5)
    assert result["effective_tick"] == 3
    assert service.last_control_output["runtime_configuration"]["control_target"] == "sigma"
    assert service.adapter.runtime_configuration()["control_target"] == "G"


def test_influx_record_uses_actual_target_and_setpoint(monkeypatch):
    service = service_running()
    records = []
    service.measurement_writer = object()
    monkeypatch.setattr(
        "crystallization_mpc.apps.controller.service.write_controller_measurement",
        lambda writer, record: records.append(record),
    )
    assert service.on_message(command({"control_target": "G", "G_set": 4e-8}))["accepted"]
    service._control_tick_once(now=105)
    assert records[-1].tags()["target"] == "G"
    assert records[-1].fields()["target_set"] == 4e-8
    assert records[-1].fields()["runtime_revision"] == 1
    assert service.on_message(command({"control_target": "sigma", "sigma_set": 0.04},
                                     event_id="switch-back", revision=1))["accepted"]
    service._control_tick_once(now=110)
    assert records[-1].tags()["target"] == "sigma"
    assert records[-1].fields()["target_set"] == 0.04
    assert records[-1].fields()["runtime_revision"] == 2
