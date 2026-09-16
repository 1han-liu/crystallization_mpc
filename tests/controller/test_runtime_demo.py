"""The repeatable four-phase numerical deliverable is itself an acceptance test."""
from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

DEMO = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/run_runtime_control_demo.py"))


@pytest.mark.parametrize("mode", ["MPC", "PI"])
def test_four_phase_demo_and_recovery(mode, tmp_path):
    report = DEMO["run_demo"](tmp_path / mode, mode)
    assert report["status"] == "PASS", report["violations"]
    assert report["ticks"] == 80
    assert report["recovery_comparisons"] == 10
    assert len(report["events"]) == 8
    assert all(event["accepted"] for event in report["events"])
    saved = json.loads((tmp_path / mode / "report.json").read_text())
    assert saved["violations"] == []
    assert saved["checkpoints"]["50"]["last_status"] == "fit_succeeded"
    assert saved["checkpoints"]["60"]["last_mode"] == "all"
    assert saved["checkpoints"]["68"]["fit_count"] == saved["checkpoints"]["62"]["fit_count"]
    assert saved["checkpoints"]["70"]["fit_count"] > saved["checkpoints"]["68"]["fit_count"]
    assert (tmp_path / mode / "cycles.csv").is_file()
