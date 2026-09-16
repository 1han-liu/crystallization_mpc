"""Real R2021a oracles: outcomes and numerical tolerance are separate gates.

Numerical differences must fail, not be xfailed or hidden by relaxed tolerances.
"""
import copy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.io import loadmat

from crystallization_mpc.apps.controller.algorithm import adaptation
from crystallization_mpc.apps.controller.algorithm.controller import CrystallizationController
from crystallization_mpc.apps.controller.tick import ControllerTickInput

FIXTURES = Path(__file__).parent / "fixtures"
ORACLE = loadmat(FIXTURES / "matlab_r2021a_adaptation_edges.mat", simplify_cells=True)
ROWS = list(ORACLE["rows"])


def invoke(row):
    return adaptation.adapt_growth_parameters(
        row["initial_params"], row["growth"], row["sigma"], row["temperature"],
        row["selected"], int(row["maximum"]), int(row["minimum"]), row["mode"])


def case_id(row):
    return f'{row["name"]}-{row["mode"]}'


@pytest.mark.parametrize("row", ROWS, ids=case_id)
def test_reference_outcome_and_input_immutability(row):
    assert ORACLE["metadata"]["release"] == "2021a"
    before = copy.deepcopy(row)
    if row["ok"]:
        _, count = invoke(row)
        assert count == row["count"]
    else:
        with pytest.raises(adaptation.AdaptationError) as caught:
            invoke(row)
        assert caught.value.reference_identifier == row["identifier"]
        assert row["params_unchanged"]
    for key in ("growth", "sigma", "temperature", "selected"):
        np.testing.assert_array_equal(row[key], before[key])
    assert row["initial_params"] == before["initial_params"]


@pytest.mark.parametrize("row", [r for r in ROWS if r["ok"]], ids=case_id)
def test_reference_numerical_tolerance(row):
    actual, _ = invoke(row)
    np.testing.assert_allclose([actual[k] for k in ("E_A", "k_0", "n")],
                               [row[k] for k in ("E_A", "k_0", "n")], rtol=1e-5, atol=0)


def test_finite_iterate_is_not_rejected_only_for_solver_exitflag(monkeypatch):
    monkeypatch.setattr(adaptation, "minimize_scalar", lambda *a, **kw:
                        SimpleNamespace(x=1., fun=0., success=False, message="iteration limit"))
    assert adaptation._bounded_minimum(lambda x: (x-1)**2, .5, 2., initial=1.) == 1.


def test_failure_partial_state_and_resume_follow_original_snippet():
    # Compare state semantics, not exact full MATLAB closed-loop trajectories.
    oracle = loadmat(FIXTURES / "matlab_r2021a_adaptation_partial_state.mat", simplify_cells=True)
    params = {"run_type": "simulation", "growth_rate_source": "simulated"}
    c = CrystallizationController()
    c.configure(params, "partial-state")
    c.start()
    c.set_adaptation(True, "all")
    for i in range(1, 62):
        if i == 11:
            c.add_seed({"event_id": "seed"})
        assert c.step(ControllerTickInput(i, 5., i*5.)).valid
    for row in oracle["rows"]:
        i = int(row["tick"])
        before_params = copy.deepcopy(c.params)
        before_filter = c.ekf_G_measure.state.copy()
        result = c.step(ControllerTickInput(i, 5., i*5.))
        assert result.valid == bool(row["ok"]) == False
        assert c.num_adapt == row["num_adapt"]
        assert len(c.history["t"]) == row["history_length"]
        assert len(c.history["G_measure_KF"]) == row["growth_history_length"]
        assert len(c.history["n"]) == row["parameter_history_length"]
        assert c.params == before_params and row["params_unchanged"]
        assert not np.array_equal(before_filter, c.ekf_G_measure.state)
        assert row["growth_filter_changed"] and not row["later_steps_executed"]
        assert c.simulation_state["T_j_set"] == c.history["T_j_set"][-1]
        assert all(getattr(result, key) is None for key in result.NUMERIC_FIELDS)
        restored = CrystallizationController()
        assert restored.restore_state(params, "partial-state", c.export_state())
        assert restored.export_state() == c.export_state()
        c = restored
    c.set_adaptation(False, "all")
    assert c.step(ControllerTickInput(66, 5., 330.)).valid
    np.testing.assert_array_equal(c.history["n"][61:65], oracle["gap_values"])
    assert {len(v) for v in c.history.values()} == {66}


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_failed_output_assignment_does_not_commit_partial_parameters(monkeypatch, fail_at):
    row = next(r for r in ROWS if r["name"] == "positive" and r["mode"] == "all")
    before = copy.deepcopy(row["initial_params"])
    original = adaptation._bounded_minimum
    calls = 0
    def failing(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise adaptation.AdaptationError("injected stage failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(adaptation, "_bounded_minimum", failing)
    with pytest.raises(adaptation.AdaptationError):
        invoke(row)
    assert calls == fail_at
    assert row["initial_params"] == before
