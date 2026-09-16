"""Operator updates must never turn into a new experiment initialization."""

from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

from crystallization_mpc.apps.controller.adapter import NoOpControllerAdapter
from crystallization_mpc.apps.controller.algorithm.controller import CrystallizationController
from crystallization_mpc.apps.controller.algorithm.integration import CrystallizationControllerAdapter
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.messaging.contracts import CONTROLLER_ADAPTATION_MODES
from crystallization_mpc.messaging.controller_runtime import (
    ControllerRuntimeUpdatePayload, validate_runtime_changes,
)

PARAMS = {"run_type": "simulation", "growth_rate_source": "simulated"}


def running_controller(mode="MPC"):
    controller = CrystallizationController()
    controller.configure({**PARAMS, "mode": mode}, "runtime-test")
    controller.start()
    for i in range(1, 4):
        controller.step(ControllerTickInput(i, 5.0, i * 5.0))
    controller.add_seed({"event_id": "pending-seed"})
    return controller


@pytest.mark.parametrize("changes", [
    {}, {"dt": 10}, {"control_target": "G+sigma"},
    {"adaptation_mode": ""}, {"adaptation_mode": []},
    {"adaptation_enabled": 1}, {"adaptation_enabled": "false"},
    *[{key: value} for key in ("sigma_set", "G_set")
      for value in (0, -1, float("nan"), float("inf"), True, "", "3e-8", None)],
])
def test_invalid_changes(changes):
    with pytest.raises(ValueError):
        validate_runtime_changes(changes)


@pytest.mark.parametrize("revision", [-1, True, 1.0, "1", None])
def test_revision_is_a_strict_nonnegative_integer(revision):
    with pytest.raises(ValueError):
        ControllerRuntimeUpdatePayload("r", "e", revision, {"control_target": "G"}, "now")


def test_wire_round_trip_and_defensive_copy():
    changes = {"control_target": "G", "G_set": 3e-8}
    command = ControllerRuntimeUpdatePayload("r", "e", 0, changes, "now")
    changes["G_set"] = -1
    assert command.changes["G_set"] == 3e-8
    assert ControllerRuntimeUpdatePayload.from_mapping(json.loads(json.dumps(command.to_dict()))) == command
    with pytest.raises(ValueError):
        ControllerRuntimeUpdatePayload.from_mapping({**command.to_dict(), "dt": 1})


@pytest.mark.parametrize("mode", ["MPC", "PI"])
def test_target_round_trip_preserves_all_numerical_state(mode):
    controller = running_controller(mode)
    before = controller.export_state()
    original_digest = controller.params_digest
    original_configured = copy.deepcopy(controller._configured_params)
    original_filter = controller.ekf
    original_history = controller.history
    # Represent already estimated kinetic values; runtime changes must keep them.
    controller.params["E_A"] = 120000.0
    controller.params["n"] = 1.3
    for target in ("G", "sigma"):
        actual = controller.update_runtime({"control_target": target})
        assert actual["control_target"] == target
        assert controller.params["target_set"] == controller.params[f"{target}_set"]
        for key in ("K_P_T", "K_I_T", "dT_dt_min", "dT_dt_max", "t_lag_threshold_perc"):
            assert controller.params[key] == controller.params[f"{key}_{target}"]
        after = controller.export_state()
        for key in before.keys() - {"params", "adaptation"}:
            assert after[key] == before[key], key
        for key in ("T_init", "steps", "seed_time"):
            assert after["params"][key] == before["params"][key]
        assert controller.params["E_A"] == 120000.0
        assert controller.params["n"] == 1.3
        assert controller.ekf is original_filter
        assert controller.history is original_history
    assert controller.params_digest == original_digest
    assert controller._configured_params == original_configured


def test_active_and_standby_setpoints_are_independent():
    controller = running_controller()
    original = controller.params["target_set"]
    controller.update_runtime({"G_set": 4e-8})
    assert controller.params["target_set"] == original
    controller.update_runtime({"sigma_set": 0.035})
    assert controller.params["target_set"] == 0.035
    controller.update_runtime({"control_target": "G"})
    assert controller.params["target_set"] == 4e-8
    controller.update_runtime({"sigma_set": 0.04})
    assert controller.params["target_set"] == 4e-8
    controller.update_runtime({"control_target": "sigma"})
    assert controller.params["target_set"] == 0.04


@pytest.mark.parametrize("mode", CONTROLLER_ADAPTATION_MODES)
def test_adaptation_selection_and_enable_preserve_state(mode):
    controller = running_controller()
    original = controller.export_state()
    controller.update_runtime({"adaptation_mode": mode})
    assert controller.adaptation_enabled is False
    controller.update_runtime({"adaptation_enabled": True})
    assert controller.adaptation_mode == mode
    controller.update_runtime({"adaptation_enabled": False})
    actual = controller.export_state()
    for key in original.keys() - {"adaptation"}:
        assert actual[key] == original[key]


def test_invalid_candidate_is_atomic():
    controller = running_controller()
    before = controller.export_state()
    with pytest.raises(ValueError):
        controller.update_runtime({"control_target": "G", "sigma_set": -1})
    assert controller.export_state() == before
    controller.params["dT_dt_min_G"] = 2
    before = controller.export_state()
    with pytest.raises(ValueError, match="inverted"):
        controller.update_runtime({"control_target": "G", "adaptation_enabled": True})
    assert controller.export_state() == before


@pytest.mark.parametrize("mode", ["MPC", "PI"])
def test_restored_runtime_target_continues_same_trajectory(mode):
    original = running_controller(mode)
    original.update_runtime({"control_target": "G", "G_set": 4e-8, "adaptation_mode": "all"})
    original.step(ControllerTickInput(4, 5.0, 20.0))
    saved = json.loads(json.dumps(original.export_state(), allow_nan=False))
    restored = CrystallizationController()
    assert restored.restore_state({**PARAMS, "mode": mode}, "runtime-test", saved)
    assert restored.runtime_configuration() == original.runtime_configuration()
    for i in range(5, 9):
        a = original.step(ControllerTickInput(i, 5.0, i * 5.0))
        b = restored.step(ControllerTickInput(i, 5.0, i * 5.0))
        assert a.valid and b.valid
        assert a.T_j_set == pytest.approx(b.T_j_set, abs=1e-10)
        assert original.history["target_set"][-1] == 4e-8


def test_adapter_explicitly_supports_or_rejects_updates():
    assert NoOpControllerAdapter().runtime_configuration() is None
    with pytest.raises(NotImplementedError):
        NoOpControllerAdapter().update_runtime({"adaptation_enabled": True})
    adapter = CrystallizationControllerAdapter(running_controller())
    assert adapter.update_runtime({"control_target": "G"})["control_target"] == "G"
    adapter.stop()
    with pytest.raises(RuntimeError, match="not running"):
        adapter.update_runtime({"control_target": "sigma"})


@pytest.mark.parametrize("mode", CONTROLLER_ADAPTATION_MODES)
def test_fitting_diagnostics_require_real_fit_and_freeze_when_disabled(mode):
    from crystallization_mpc.apps.controller.algorithm import adaptation

    controller = CrystallizationController()
    controller.configure({**PARAMS, "c_init": 0.4}, "runtime-test")
    controller.start()
    controller.update_runtime({"adaptation_enabled": True, "adaptation_mode": mode})
    with patch.object(adaptation, "_bounded_minimum", wraps=adaptation._bounded_minimum) as solver:
        for i in range(1, 45):
            output = controller.step(ControllerTickInput(i, 5.0, i * 5.0))
            assert output.valid
            if controller.num_adapt < 30:
                assert solver.call_count == 0
                assert controller.adaptation_status()["fit_count"] == 0
        assert solver.call_count > 0
        diagnostic = controller.adaptation_status()
        assert diagnostic["last_status"] == "fit_succeeded"
        assert diagnostic["fit_count"] > 0
        assert diagnostic["failure_count"] == 0
        assert diagnostic["last_mode"] == mode
        initial = controller._configured_params
        selected = {
            "E_A": {"E_A"}, "k_0": {"k_0"}, "n": {"n"},
            "E_A_and_k_0": {"E_A", "k_0"}, "E_A_and_n": {"E_A", "n"},
            "k_0_and_n": {"k_0", "n"}, "all": {"E_A", "k_0", "n"},
        }[mode]
        for key in {"E_A", "k_0", "n"} - selected:
            assert controller.params[key] == initial[key]
        assert solver.call_count == diagnostic["fit_count"] * len(selected)
        controller.update_runtime({"adaptation_enabled": False})
        calls_before = solver.call_count
        controller.step(ControllerTickInput(45, 5.0, 225.0))
        assert solver.call_count == calls_before
        assert controller.adaptation_status()["fit_count"] == diagnostic["fit_count"]
        controller.update_runtime({"adaptation_enabled": True})
        controller.step(ControllerTickInput(46, 5.0, 230.0))
        assert solver.call_count > calls_before


def test_original_default_domain_failure_is_reported_not_hidden():
    controller = CrystallizationController()
    controller.configure(PARAMS, "runtime-test")
    controller.start()
    controller.update_runtime({"adaptation_enabled": True})
    for i in range(1, 81):
        if i == 11:
            controller.add_seed({"event_id": "seed"})
        output = controller.step(ControllerTickInput(i, 5.0, i * 5.0))
        if not output.valid:
            diagnostic = controller.adaptation_status()
            assert diagnostic["last_status"] == "fit_failed"
            assert diagnostic["failure_count"] > 0
            assert diagnostic["pause_reason"] == output.error
            break
    else:
        pytest.fail("Expected the preserved default-input MATLAB domain failure.")


def test_active_explicit_target_set_is_reported_truthfully():
    controller = CrystallizationController()
    controller.configure({**PARAMS, "target_set": 0.04, "sigma_set": 0.12}, "runtime-test")
    controller.start()
    assert controller.runtime_configuration()["sigma_set"] == 0.04
    controller.update_runtime({"adaptation_mode": "n"})
    assert controller.params["target_set"] == 0.04
    controller.update_runtime({"control_target": "G"})
    controller.update_runtime({"control_target": "sigma"})
    assert controller.params["target_set"] == 0.04
