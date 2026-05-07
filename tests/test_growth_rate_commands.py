import shutil
from pathlib import Path
from uuid import uuid4

from crystallization_mpc.apps.central.params import load_params, save_params_document
from crystallization_mpc.apps.central.ui.app import CentralApp
from crystallization_mpc.apps.gsensor.app import GsensorService


def _make_test_param_dir() -> Path:
    path = Path(__file__).resolve().parent / ".tmp_gsensor_params" / uuid4().hex
    path.mkdir(parents=True)
    return path


def test_central_builds_growth_rate_start_command():
    app = CentralApp()

    msg = app.build_growth_rate_command(True, seq=123)

    assert msg["src"] == "central"
    assert msg["dst"] == "gsensor"
    assert msg["msg_type"] == "command"
    assert msg["name"] == "growth_rate.start"
    assert msg["seq"] == 123
    assert msg["payload"] == {"key": "G_active", "active": True}


def test_central_builds_growth_rate_stop_command():
    app = CentralApp()

    msg = app.build_growth_rate_command(False, seq=124)

    assert msg["dst"] == "gsensor"
    assert msg["msg_type"] == "command"
    assert msg["name"] == "growth_rate.stop"
    assert msg["seq"] == 124
    assert msg["payload"] == {"key": "G_active", "active": False}


def test_gsensor_updates_params_from_message():
    service = GsensorService()

    service.on_message(
        {
            "msg_type": "params",
            "name": "update",
            "payload": {"params": {"dt_G": 15, "params_G.width": 100}},
        }
    )

    assert service.params["dt_G"] == 15
    assert service.params["params_G.width"] == 100
    assert service.active is False
    assert service.initialized is False


def test_gsensor_start_and_stop_commands_update_state():
    service = GsensorService()

    service.on_message(
        {
            "msg_type": "command",
            "name": "growth_rate.start",
            "payload": {"key": "G_active", "active": True},
        }
    )

    assert service.active is True
    assert service.initialized is True
    assert service.initialization_status == "placeholder_ready"
    assert service.initialized_at is not None
    assert service.status()["measurement_running"] is True
    assert service.status()["last_command_message"]["name"] == "growth_rate.start"

    service.on_message(
        {
            "msg_type": "command",
            "name": "growth_rate.stop",
            "payload": {"key": "G_active", "active": False},
        }
    )

    assert service.active is False
    assert service.initialized is True
    assert service.status()["measurement_running"] is False
    assert service.status()["last_command_message"]["name"] == "growth_rate.stop"


def test_gsensor_ui_params_update_memory_and_runtime_file():
    workdir = _make_test_param_dir()
    try:
        default_path = workdir / "params_default.yaml"
        runtime_path = workdir / "params_runtime.yaml"
        meta_path = workdir / "param_meta.yaml"
        save_params_document(
            str(default_path),
            version=1,
            shared={"dt": 5, "dt_G": 15},
            gsensor={"params_G.width": 100},
            controller={"sigma_set": 0.035},
        )
        meta_path.write_text("version: 1\nparams: {}\n", encoding="utf-8")
        default_before = default_path.read_text(encoding="utf-8")
        service = GsensorService(
            default_params_path=default_path,
            runtime_params_path=runtime_path,
            param_meta_path=meta_path,
        )

        service.apply_ui_params({"dt_G": 20, "params_G.width": 120}, version=1)

        assert service.params["dt_G"] == 20
        assert service.params["params_G.width"] == 120
        assert runtime_path.exists()
        assert default_path.read_text(encoding="utf-8") == default_before

        shared, gsensor, controller, version = load_params(str(runtime_path))
        assert version == 1
        assert shared["dt"] == 5
        assert shared["dt_G"] == 20
        assert gsensor["params_G.width"] == 120
        assert controller == {"sigma_set": 0.035}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_gsensor_ui_params_preserve_controller_values():
    workdir = _make_test_param_dir()
    try:
        default_path = workdir / "params_default.yaml"
        runtime_path = workdir / "params_runtime.yaml"
        meta_path = workdir / "param_meta.yaml"
        save_params_document(
            str(default_path),
            version=1,
            shared={"dt": 5},
            gsensor={"params_G.width": 100},
            controller={"sigma_set": 0.035, "params.K_u": 15},
        )
        save_params_document(
            str(runtime_path),
            version=2,
            shared={"dt": 6},
            gsensor={"params_G.width": 110},
            controller={"sigma_set": 0.04, "params.K_u": 18},
        )
        meta_path.write_text("version: 1\nparams: {}\n", encoding="utf-8")
        service = GsensorService(
            default_params_path=default_path,
            runtime_params_path=runtime_path,
            param_meta_path=meta_path,
        )

        service.apply_ui_params({"dt": 7, "params_G.width": 130}, version=2)

        shared, gsensor, controller, version = load_params(str(runtime_path))
        assert version == 2
        assert shared == {"dt": 7}
        assert gsensor == {"params_G.width": 130}
        assert controller == {"sigma_set": 0.04, "params.K_u": 18}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_gsensor_ui_reset_restores_default_gsensor_params_only():
    workdir = _make_test_param_dir()
    try:
        default_path = workdir / "params_default.yaml"
        runtime_path = workdir / "params_runtime.yaml"
        meta_path = workdir / "param_meta.yaml"
        save_params_document(
            str(default_path),
            version=1,
            shared={"dt": 5, "dt_G": 15},
            gsensor={"params_G.width": 100},
            controller={"sigma_set": 0.035},
        )
        save_params_document(
            str(runtime_path),
            version=2,
            shared={"dt": 7, "dt_G": 20},
            gsensor={"params_G.width": 130},
            controller={"sigma_set": 0.04},
        )
        meta_path.write_text("version: 1\nparams: {}\n", encoding="utf-8")
        service = GsensorService(
            default_params_path=default_path,
            runtime_params_path=runtime_path,
            param_meta_path=meta_path,
        )

        payload = service.reset_ui_params_to_default()

        assert payload["params"]["dt"] == 5
        assert payload["params"]["dt_G"] == 15
        assert payload["params"]["params_G.width"] == 100
        assert service.params["dt_G"] == 15
        assert service.params["params_G.width"] == 100

        shared, gsensor, controller, version = load_params(str(runtime_path))
        assert version == 1
        assert shared == {"dt": 5, "dt_G": 15}
        assert gsensor == {"params_G.width": 100}
        assert controller == {"sigma_set": 0.04}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
