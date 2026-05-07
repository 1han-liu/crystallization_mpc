from crystallization_mpc.apps.central.ui.app import CentralApp
from crystallization_mpc.apps.gsensor.app import GsensorService


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
