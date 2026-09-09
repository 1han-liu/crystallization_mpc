"""Cover persistence across the actual experiment-start and first-image path."""
import json
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from crystallization_mpc.apps.gsensor.app import GsensorService
from crystallization_mpc.apps.gsensor.experiments import GsensorExperimentManager


@pytest.fixture
def manager(tmp_path):
    manager = GsensorExperimentManager(tmp_path / "experiments")
    run = manager.registry.create(label="startup-regression")
    manager.select({"run_id": run.run_id, "image_directory": "images", "mode": "live"}, running=False)
    return manager, run.run_id


@pytest.mark.parametrize("version", [1])
def test_processing_state_supported_versions_round_trip(manager, version):
    store, run_id = manager
    state = {"schema_version": version, "run_id": run_id, "lifecycle_status": "selected"}
    store.save_processing_state(run_id, state)
    assert store.load_processing_state(run_id) == state
    # Selection is a separate document: its schema remains version 1.
    assert json.loads(store.state_path.read_text())["schema_version"] == 1


@pytest.mark.parametrize("version", [0, 2, 3, 999, None, True, 1.0, "1"])
def test_unsupported_versions_rejected_without_overwriting(manager, version):
    store, run_id = manager
    valid = {"schema_version": 1, "run_id": run_id}
    path = store.save_processing_state(run_id, valid)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        store.save_processing_state(run_id, {**valid, "schema_version": version})
    assert path.read_bytes() == before
    path.write_text(json.dumps({**valid, "schema_version": version}))
    with pytest.raises(ValueError):
        store.load_processing_state(run_id)


@pytest.mark.parametrize("version", [1])
def test_processing_state_rejects_wrong_run(manager, version):
    store, run_id = manager
    with pytest.raises(ValueError, match="run_id"):
        store.save_processing_state(run_id, {"schema_version": version, "run_id": "wrong"})
    store.processing_state_path(run_id).write_text(json.dumps({"schema_version": version, "run_id": "wrong"}))
    with pytest.raises(ValueError, match="run_id"):
        store.load_processing_state(run_id)


def test_nested_processor_version_two_is_also_rejected(manager):
    store, run_id = manager
    state = {"schema_version": 1, "run_id": run_id,
             "processor": {"schema_version": 2, "run_id": run_id}}
    with pytest.raises(ValueError, match="schema_version"):
        store.save_processing_state(run_id, state)
    store.processing_state_path(run_id).write_text(json.dumps(state))
    with pytest.raises(ValueError, match="schema"):
        store.load_processing_state(run_id)


@pytest.mark.parametrize("restore_version", [1])
def test_start_scans_first_image_and_restores_initialization(manager, tmp_path, monkeypatch, restore_version):
    def deny_network(*args, **kwargs):
        raise AssertionError("Startup regression must not contact services or devices")
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    store, run_id = manager
    for name in ("IMG_00517.PNG", "IMG_00518.PNG"):
        Image.new("RGB", (64, 48), "gray").save(store.registry.image_dir(run_id) / name)
    kwargs = dict(experiment_root_path=store.registry.root,
                  runtime_params_path=tmp_path / "params.yaml",
                  status_publish_enabled=False, sample_publish_enabled=False, influx_enabled=False)
    service = GsensorService(**kwargs)
    # Run the real scan synchronously, without starting a consumer or racing a thread.
    scan_start = Mock()
    monkeypatch.setattr(service, "start_image_scanning", scan_start)
    message = {"src": "central", "dst": "gsensor", "msg_type": "command", "name": "experiment.start",
               "payload": {"run_id": run_id, "parameter_version": 1, "started_at": "2026-09-09T15:00:00Z",
                           "image_directory": "images", "mode": "live"}}
    service.on_message(message)
    assert service.experiment_lifecycle_status == "waiting_for_initial_image", service.experiment_lifecycle_error
    scan_start.assert_called_once()
    assert store.load_processing_state(run_id)["schema_version"] == 1
    service._scan_current_image_directory()
    first = service.initialization.payload()
    assert service.experiment_lifecycle_status == "initializing"
    assert first["status"] == "awaiting_is_full"
    assert Path(first["selected_image"]).name == "IMG_00517.PNG"
    assert len(service.processed_image_files) == 1
    state = store.load_processing_state(run_id)
    assert state["initialization"]["session_id"] == first["session_id"]
    state["schema_version"] = restore_version
    store.save_processing_state(run_id, state)
    restored = GsensorService(**kwargs)
    assert restored.recovery_status == "restored", restored.recovery_error
    assert restored.initialization.payload()["session_id"] == first["session_id"]
    assert restored.experiment_lifecycle_status == "initializing"
    # A repeated start must not discard the pending manual marking session.
    restored.on_message(message)
    assert restored.initialization.payload()["session_id"] == first["session_id"]
    assert len(restored.processed_image_files) == 1
    service.close()
    restored.close()
