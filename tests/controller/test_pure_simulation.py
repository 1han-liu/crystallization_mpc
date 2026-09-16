"""Image-free audit coverage. Characterized failures are not acceptance passes."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

from crystallization_mpc.apps.controller.algorithm import adaptation
from crystallization_mpc.apps.controller.algorithm.controller import CrystallizationController
from crystallization_mpc.apps.controller.algorithm.dynamics import state_transition_function_T
from crystallization_mpc.apps.controller.algorithm.control import update_T_j
from crystallization_mpc.apps.controller.algorithm.integration import CrystallizationControllerAdapter
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.apps.controller.service import ControllerService
from crystallization_mpc.apps.controller.tick import ControllerTickInput

spec = importlib.util.spec_from_file_location(
    "numerical_audit", Path(__file__).resolve().parents[2] / "scripts/run_controller_simulation.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture
def params():
    return {"run_type": "simulation", "growth_rate_source": "simulated", "dt": 5.0,
            "simulation_noise": audit.fixed_inputs(100)}


@pytest.mark.parametrize("mode", adaptation.ADAPTATION_MODES)
def test_positive_supersaturation_reaches_real_fit_without_premature_updates(params, mode):
    with audit.no_network() as attempted:
        run = audit.execute({**params, "c_init": .4}, 45, adaptive=mode, seed_tick=11)
    summary = audit.summarize(run, adaptive=True)
    assert summary["status"] == "PASS", summary
    assert summary["first_fit_tick"] == 30
    assert all(row["fit_calls"] == 0 for row in run["records"][:29])
    assert not attempted


@pytest.mark.parametrize("mode", adaptation.ADAPTATION_MODES)
def test_default_adaptation_failure_is_reported_not_masked_as_success(params, mode):
    with audit.no_network():
        run = audit.execute(params, 80, adaptive=mode, seed_tick=11)
    summary = audit.summarize(run, adaptive=True)
    assert summary["status"] == "FAIL"
    assert summary["invalid_ticks"] > 0
    assert summary["first_failure"]["error"] == "Objective function is undefined at initial point. Fmincon cannot continue."
    invalid = [r for r in run["records"] if r["status"] == "invalid"]
    assert all(row["T_j_set"] is None for row in invalid)
    assert not run["violations"]


def test_repeat_and_json_recovery_with_adaptation_and_seed(params):
    params = {**params, "c_init": .4}
    with audit.no_network():
        continuous = audit.execute(params, 60, adaptive="all", seed_tick=11)
        repeated = audit.execute(params, 60, adaptive="all", seed_tick=11)
        recovered = audit.execute(params, 60, adaptive="all", seed_tick=11, resume_at=40)
    assert audit.canonical_records(continuous) == audit.canonical_records(repeated)
    assert audit.canonical_records(continuous) == audit.canonical_records(recovered)
    assert continuous["state"] == recovered["state"]
    broken = copy.deepcopy(continuous["state"])
    del broken["filters"]
    c = CrystallizationController()
    assert not c.restore_state(params, "numerical-audit", broken)
    assert not c.running


def test_plant_step_uses_previous_control_and_conserves_mass(params):
    c = audit.make_controller(params, "plant")
    c.step(ControllerTickInput(1, 5., 5.))
    c.add_seed({"event_id": "seed-once"})
    previous = dict(c.simulation_state)
    initial_mass = audit.mass(c, c.size_list_seed)
    expected_T = state_transition_function_T(c.params, previous["T"], previous["T_j"], 5.) + params["simulation_noise"]["T_noise"][1]
    p = c.params
    expected_T_j = update_T_j(previous["T_j_set"], previous["T_j"], p["dT_j_dt_max"], 5., p["dT_j"], p["dt_update"], p["T_j_min"], p["T_j_max"])
    result = c.step(ControllerTickInput(2, 5., 10.))
    assert result.valid
    assert result.T == pytest.approx(expected_T, abs=1e-12)
    assert result.T_j == pytest.approx(expected_T_j, abs=1e-12)
    assert audit.mass(c) - p["m_solvent"] * params["simulation_noise"]["c_noise"][1] == pytest.approx(initial_mass, abs=1e-12)
    assert c.simulation_state["T_j_set"] == result.T_j_set
    assert c.mark_seed and not c.pending_seed


@pytest.mark.parametrize("field,value", [("dt", 0), ("dt", float("nan")), ("m_solvent", -1), ("mode", "unknown")])
def test_illegal_configuration_is_rejected(params, field, value):
    with pytest.raises(ValueError):
        audit.make_controller({**params, field: value}, "invalid")


def test_adaptation_solver_failure_preserves_prior_cycle_work(params, monkeypatch):
    c = audit.make_controller({**params, "c_init": .4}, "failure", "all")
    for i in range(1, 30):
        assert c.step(ControllerTickInput(i, 5., i * 5.)).valid
    history_before = copy.deepcopy(c.history)
    params_before = copy.deepcopy(c.params)
    def fail(*args, **kwargs):
        raise adaptation.AdaptationError("injected adaptation optimizer failure")
    monkeypatch.setattr(adaptation, "_bounded_minimum", fail)
    result = c.step(ControllerTickInput(30, 5., 150.))
    assert not result.valid and result.error == "injected adaptation optimizer failure"
    assert all(getattr(result, field) is None for field in result.NUMERIC_FIELDS)
    assert c.params == params_before
    assert len(c.history["t"]) == 30
    assert c.history["n"] == history_before["n"]
    assert len(c.history["G_measure_KF"]) == 30
    assert c.simulation_state["T_j_set"] == c.history["T_j_set"][-1]


def test_mock_service_seed_dedup_and_zero_process_io(params):
    # Instantiate only: no consumer thread, web server, or broker is started.
    settings = ControllerSettings(rabbit_url="amqp://unused", rabbit_exchange="unused",
        rabbit_queue="unused", adapter_spec=None, opcua_enabled=False, opcua_endpoint=None,
        influx_enabled=False, influx_url="", influx_org="", influx_bucket="", opcua_write_enabled=False)
    class ForbiddenProcess:
        calls = 0
        def connect(self):
            self.calls += 1
            raise AssertionError("device access")
        read_state = connect
        write_jacket_setpoint = connect
        def disconnect(self):
            pass
        def status(self):
            return {"connected": False, "read_count": 0, "write_count": 0}
    process = ForbiddenProcess()
    adapter = CrystallizationControllerAdapter()
    with audit.no_network() as attempted:
        service = ControllerService(settings, adapter=adapter, process_adapter=process)
        service._apply_parameters({"version": 1, "params": params})
        service._start_experiment({"run_id": "mock-numeric", "parameter_version": 1,
                                   "started_at": "2026-09-09T00:00:00Z"})
        seed = {"run_id": "mock-numeric", "event_id": "seed-1", "added_at": "2026-09-09T00:00:01Z"}
        service._add_seed(seed)
        assert service._add_seed(seed)["duplicate"]
        assert service.seed_event_count == 1 and service.duplicate_seed_event_count == 1
        for _ in range(5):
            service._control_tick_once()
        assert service.control_output_count == 5
        service._stop_experiment({"run_id": "mock-numeric", "stopped_at": "2026-09-09T00:00:30Z"})
        assert not service._control_tick_once()["executed"]
    assert not attempted and process.calls == 0
