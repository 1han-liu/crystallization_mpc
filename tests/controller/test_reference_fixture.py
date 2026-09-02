from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.io import loadmat


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads(
    (ROOT / "tests/controller/fixtures/baseline_manifest.json").read_text(
        encoding="utf-8"
    )
)
FIXTURE = ROOT / MANIFEST["fixtures"][0]["path"]


def test_mat_fixture_has_recorded_hash_and_baseline() -> None:
    expected = MANIFEST["fixtures"][0]["sha256"]
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == expected
    data = loadmat(FIXTURE, simplify_cells=True)
    assert data["metadata"]["baseline_commit"] == MANIFEST["baseline"]["commit"]
    assert "Update 8" in data["metadata"]["matlab_version"]


def test_mat_fixture_covers_modes_targets_and_adaptation() -> None:
    data = loadmat(FIXTURE, simplify_cells=True)
    mode_targets = {(row["mode"], row["target"]) for row in data["mode_target"]}
    assert mode_targets == {
        ("MPC", "sigma"),
        ("MPC", "G"),
        ("PI", "sigma"),
        ("PI", "G"),
    }
    assert [row["mode"] for row in data["adaptation"]] == MANIFEST["enums"][
        "adaptation_mode"
    ]
    assert data["rng_data"]["G_noise"].shape == (40, 4)
    assert np.isfinite(data["rng_data"]["G_noise"]).all()


def test_fixture_contains_trajectory_state_and_covariance() -> None:
    data = loadmat(FIXTURE, simplify_cells=True)
    assert data["ekf"]["states"].shape == (4, 4)
    assert data["ekf"]["covariances"].shape == (4, 4, 4)
    assert data["general_ekf"]["filtered"].shape == (5,)
    assert np.isfinite(data["ekf"]["states"]).all()
    assert np.isfinite(data["ekf"]["covariances"]).all()
    seed = data["seed_population"]
    assert seed["sizes"].ndim == 1
    assert len(seed["sizes"]) > 1000
    assert np.isfinite(seed["sizes"]).all()
    assert seed["total_mass"] == pytest.approx(seed["requested_mass"], rel=0.02)
