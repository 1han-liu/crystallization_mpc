from __future__ import annotations

import json

import pytest

from crystallization_mpc.apps.controller.translated.thermodynamics import calc_c_sat
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.apps.controller.translated.matlab_controller import (
    MatlabController,
)
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload


def growth_sample(frame: int, *, value: float = 3e-8) -> GrowthRateSamplePayload:
    return GrowthRateSamplePayload(
        run_id="run-1",
        frame_seq=frame,
        image_name=f"frame-{frame}.png",
        captured_at="2026-08-26T12:00:00Z",
        processed_at="2026-08-26T12:00:01Z",
        dt_s=15.0,
        valid=True,
        status="measuring",
        G_u=value,
        G_u_KF=value,
        G_v=value,
        G_v_KF=value,
    )


def test_simulation_runs_without_process_state_and_uses_controller_clock() -> None:
    controller = MatlabController()
    controller.configure(
        {"run_type": "simulation", "growth_rate_source": "simulated"}, "run-1"
    )
    controller.start()
    results = [
        controller.step(ControllerTickInput(index, 5.0, index * 5.0))
        for index in range(1, 5)
    ]
    assert all(isinstance(result, ControllerStepResult) and result.valid for result in results)
    assert controller.frame_index == 4
    assert controller.history["t"] == [5.0, 10.0, 15.0, 20.0]
    assert all(result.T_j_set is not None for result in results)


def test_growth_staleness_pauses_adaptation_but_not_control() -> None:
    controller = MatlabController()
    controller.configure(
        {"run_type": "simulation", "growth_rate_source": "live_gsensor"}, "run-1"
    )
    controller.set_adaptation(True, "all")
    controller.start()
    fresh = controller.step(
        ControllerTickInput(1, 5.0, 5.0, growth_sample(1), 0.0)
    )
    stale = controller.step(
        ControllerTickInput(2, 5.0, 10.0, growth_sample(1), 31.0)
    )
    assert fresh is not None and fresh.valid and fresh.T_j_set is not None
    assert stale is not None and stale.valid and stale.T_j_set is not None
    assert controller.history["to_adapt"] == [True, False]
    assert controller.last_adaptation_pause_reason == "stale_growth_sample"


def test_seed_event_changes_simulation_population_on_next_tick() -> None:
    controller = MatlabController()
    controller.configure(
        {"run_type": "simulation", "growth_rate_source": "simulated"}, "run-1"
    )
    controller.start()
    controller.step(ControllerTickInput(1, 5.0, 5.0))
    assert not controller.size_list.any()
    controller.add_seed({"event_id": "seed-1"})
    controller.step(ControllerTickInput(2, 5.0, 10.0))
    assert controller.mark_seed is True
    assert controller.pending_seed is False
    assert controller.size_list.any()


def test_stop_forbids_further_calculation() -> None:
    controller = MatlabController()
    controller.configure(
        {"run_type": "simulation", "growth_rate_source": "simulated"}, "run-1"
    )
    controller.start()
    controller.stop()
    with pytest.raises(RuntimeError, match="not running"):
        controller.step(ControllerTickInput(1, 5.0, 5.0))


def test_state_export_is_json_safe_and_restore_is_continuous() -> None:
    params = {"run_type": "simulation", "growth_rate_source": "simulated"}
    original = MatlabController()
    original.configure(params, "run-1")
    original.start()
    for index in range(1, 4):
        original.step(ControllerTickInput(index, 5.0, index * 5.0))
    state = original.export_state()
    assert state is not None
    json.dumps(state, allow_nan=False)

    restored = MatlabController()
    assert restored.restore_state(params, "run-1", state)
    original_next = original.step(ControllerTickInput(4, 5.0, 20.0))
    restored_next = restored.step(ControllerTickInput(4, 5.0, 20.0))
    assert original_next is not None and restored_next is not None
    assert restored_next.T_j_set == pytest.approx(original_next.T_j_set, abs=1e-10)
    assert restored_next.T_KF == pytest.approx(original_next.T_KF, abs=1e-10)
    assert restored_next.c_KF == pytest.approx(original_next.c_KF, abs=1e-12)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("algorithm_state_schema_version", 2),
        ("baseline_commit", "wrong"),
        ("run_id", "other-run"),
        ("params_digest", "wrong"),
    ],
)
def test_restore_rejects_incompatible_state(field: str, value: object) -> None:
    params = {"run_type": "simulation", "growth_rate_source": "simulated"}
    source = MatlabController()
    source.configure(params, "run-1")
    source.start()
    state = dict(source.export_state() or {})
    state[field] = value
    target = MatlabController()
    assert target.restore_state(params, "run-1", state) is False
    assert target.running is False


def test_invalid_physical_process_input_returns_invalid_result() -> None:
    # The tick dataclass validates time. Numerical/physical process failures
    # are represented by an invalid result rather than an exception.
    controller = MatlabController()
    controller.configure(
        {"run_type": "simulation", "growth_rate_source": "simulated", "c_init": -1.0},
        "run-1",
    )
    controller.start()
    result = controller.step(ControllerTickInput(1, 5.0, 5.0))
    assert result is not None and not result.valid
    assert result.error == "Process input violates physical bounds."


def test_zero_supersaturation_first_tick_is_recoverable_warmup_none() -> None:
    c_sat = float(calc_c_sat(315.15))
    params = {
        "run_type": "simulation",
        "growth_rate_source": "simulated",
        "c_init": c_sat,
    }
    controller = MatlabController()
    controller.configure(params, "run-1")
    controller.start()
    assert controller.step(ControllerTickInput(1, 5.0, 5.0)) is None
    assert {len(values) for values in controller.history.values()} == {1}
    state = controller.export_state()
    assert state is not None
    restored = MatlabController()
    assert restored.restore_state(params, "run-1", state)


def test_expected_optimizer_failure_returns_invalid_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from crystallization_mpc.apps.controller.translated import control

    class Failed:
        success = False
        x = float("nan")
        message = "forced failure"

    monkeypatch.setattr(control, "minimize_scalar", lambda *args, **kwargs: Failed())
    controller = MatlabController()
    controller.configure(
        {"run_type": "simulation", "growth_rate_source": "simulated"}, "run-1"
    )
    controller.start()
    result = controller.step(ControllerTickInput(1, 5.0, 5.0))
    assert result is not None and result.valid is False
    assert result.error == "MPC optimization failed: forced failure"
    assert all(getattr(result, field) is None for field in result.NUMERIC_FIELDS)
    second = controller.step(ControllerTickInput(2, 5.0, 10.0))
    assert second is not None and second.valid is False
    assert controller.frame_index == 2
    assert {len(values) for values in controller.history.values()} == {0}


def test_restore_rebuilds_ekf_closures_from_adapted_parameters() -> None:
    params = {
        "run_type": "simulation",
        "growth_rate_source": "live_gsensor",
        "min_num_adapt": 1,
        "c_init": 0.4,
    }
    original = MatlabController()
    original.configure(params, "run-1")
    original.set_adaptation(True, "E_A")
    original.start()
    for index in range(1, 3):
        original.step(
            ControllerTickInput(
                index,
                5.0,
                index * 5.0,
                growth_sample(index, value=3e-8),
                0.0,
            )
        )
    state = original.export_state()
    assert state is not None
    restored = MatlabController()
    assert restored.restore_state(params, "run-1", state)
    assert restored.params["E_A"] == pytest.approx(original.params["E_A"])
    expected = original.step(
        ControllerTickInput(3, 5.0, 15.0, growth_sample(3, value=3e-8), 0.0)
    )
    actual = restored.step(
        ControllerTickInput(3, 5.0, 15.0, growth_sample(3, value=3e-8), 0.0)
    )
    assert expected is not None and actual is not None
    assert actual.T_KF == pytest.approx(expected.T_KF, abs=1e-10)
    assert actual.c_KF == pytest.approx(expected.c_KF, abs=1e-12)
    assert actual.E_A == pytest.approx(expected.E_A, rel=1e-10)


def test_temporary_missing_experiment_state_does_not_break_next_tick() -> None:
    from crystallization_mpc.apps.controller.process import ProcessState

    controller = MatlabController()
    controller.configure(
        {"run_type": "experiment", "growth_rate_source": "live_gsensor"}, "run-1"
    )
    controller.start()
    invalid = controller.step(
        ControllerTickInput(1, 5.0, 5.0, growth_sample(1), 0.0)
    )
    assert invalid is not None and invalid.valid is False
    process = ProcessState(
        T=306.15,
        T_j=305.15,
        c=0.31,
        count_middle=100.0,
        T_j_set=304.15,
        read_at="2026-08-26T12:00:00Z",
    )
    valid = controller.step(
        ControllerTickInput(2, 5.0, 10.0, growth_sample(1), 5.0, process)
    )
    assert valid is not None and valid.valid
    assert controller.frame_index == 2
    assert {len(values) for values in controller.history.values()} == {1}
