from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from crystallization_mpc.apps.central.run_configuration import RunConfiguration


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tests/controller/fixtures/baseline_manifest.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _yaml_defaults(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    result: dict[str, object] = {}
    for section in document["params"].values():
        result.update({item["key"]: item["default"] for item in section})
    return result


def test_baseline_is_frozen_to_approved_commit(manifest: dict) -> None:
    assert manifest["schema_version"] == 1
    assert manifest["baseline"]["branch"] == "main"
    assert (
        manifest["baseline"]["commit"]
        == "ce885a13e0a3e95ac0509e06eebf9d7cd1d418b0"
    )
    assert manifest["baseline"]["matlab_release"] == "R2021a Update 8"


def test_controller_source_traceability_is_complete_and_matches_reference(
    manifest: dict,
) -> None:
    traceability = manifest["traceability"]
    groups = traceability["source_groups"]
    assert set(groups) == {
        "convert",
        "split",
        "contract",
        "platform",
        "non-production",
        "canonical-duplicate",
        "backup",
    }
    classified = [path for paths in groups.values() for path in paths]
    assert len(classified) == traceability["expected_source_count"] == 87
    assert len(classified) == len(set(classified))
    assert "source_codes/gui_main/op_section.m" in groups["contract"]

    reference_root = Path(manifest["baseline"]["reference_worktree"])
    if not reference_root.is_dir():
        pytest.skip("Frozen MATLAB reference worktree is not present on this machine.")
    discovered = {
        "source_codes/controller_for_gui.m",
        "source_codes/parameters.m",
        "source_codes/parameters_on_target_change.m",
        "source_codes/parameters_G.m",
        "source_codes/calc_mode.m",
        "source_codes/gui_main/op_section.m",
    }
    discovered.update(
        path.relative_to(reference_root).as_posix()
        for path in (reference_root / "source_codes/subroutines_controller").rglob("*")
        if path.is_file() and path.suffix in {".m", ".asv"}
    )
    discovered.update(
        path.relative_to(reference_root).as_posix()
        for path in (
            reference_root / "source_codes/gui_main/snipptets_controller"
        ).iterdir()
        if path.is_file() and path.suffix in {".m", ".asv"}
    )
    assert set(classified) == discovered


def test_manifest_declares_all_seven_adaptation_modes(manifest: dict) -> None:
    assert manifest["enums"]["adaptation_mode"] == [
        "E_A",
        "k_0",
        "n",
        "E_A_and_k_0",
        "E_A_and_n",
        "k_0_and_n",
        "all",
    ]


def test_operation_contract_preserves_matlab_options_and_explicit_safe_defaults(
    manifest: dict,
) -> None:
    contract = manifest["operation_contract"]
    options = contract["matlab_options"]
    assert options["mode"] == manifest["enums"]["mode"]
    assert options["exp_sim"] == manifest["enums"]["run_type"]
    assert options["target"] == manifest["enums"]["target"]
    assert options["adaptive_mode"] == manifest["enums"]["adaptation_mode"]
    source_mapping = contract["python_growth_source_mapping"]
    assert [source_mapping[value] for value in options["exp_sim_G"]] == [
        "simulated",
        "live_gsensor",
        "presaved_images",
    ]
    assert set(source_mapping.values()) == set(manifest["enums"]["growth_rate_source"])
    assert RunConfiguration().to_dict() == contract["central_safe_default"]
    assert contract["runtime_value_source"] == (
        "parameters base workspace via OperationsTab evalin"
    )


def test_manifest_has_unique_matlab_and_python_parameter_keys(manifest: dict) -> None:
    parameters = manifest["parameters"]
    matlab_keys = [item["matlab"] for item in parameters]
    python_keys = [item["python"] for item in parameters]
    assert len(matlab_keys) == len(set(matlab_keys))
    assert len(python_keys) == len(set(python_keys))
    for item in parameters:
        assert item["source"] in {"parameters.m", "parameters_G.m"}
        assert item["owner"] in {"controller", "shared", "gsensor"}
        assert item["unit"]
        assert ("default" in item) ^ ("formula" in item)


@pytest.mark.parametrize("path", [ROOT / "params_default.yaml", ROOT / "params_runtime.yaml"])
def test_corrected_central_defaults_match_matlab(path: Path) -> None:
    defaults = _yaml_defaults(path)
    assert defaults["sigma_set"] == pytest.approx(0.12)
    assert defaults["min_num_adapt"] == 30
    assert defaults["max_num_adapt"] == 1000
    assert defaults["T_init_G"] == pytest.approx(311.65)


def test_timing_and_unit_contract(manifest: dict) -> None:
    runtime = manifest["runtime"]
    assert runtime["controller_dt_s"] == pytest.approx(5.0)
    assert runtime["growth_dt_s"] == pytest.approx(15.0)
    assert runtime["growth_stale_after_s"] == pytest.approx(30.0)
    assert runtime["temperature_unit"] == "K"
    assert runtime["growth_rate_unit"] == "m/s"
    assert runtime["concentration_unit"] == "g/gH2O"


def test_deviation_ledger_has_required_reference_anomalies(manifest: dict) -> None:
    deviations = {item["id"]: item for item in manifest["deviations"]}
    assert deviations["D-001"]["source"] == "calc_mode.m"
    assert deviations["D-001"]["decision"] == "reproduce"
    assert deviations["D-002"]["source"] == "calc_T_j_set_.m"
    assert deviations["D-002"]["decision"] == "reproduce"
    assert deviations["D-004"]["source"] == "op_section.m and OperationsTab.m"
    assert "explicit Central safe run configuration" in deviations["D-004"][
        "decision"
    ]


@pytest.mark.parametrize("path", [ROOT / "params_default.yaml", ROOT / "params_runtime.yaml"])
def test_all_static_controller_and_shared_defaults_match_manifest(
    manifest: dict, path: Path
) -> None:
    defaults = _yaml_defaults(path)
    runtime_only = {
        "mode",
        "run_type",
        "target",
        "adaptation_mode",
        "growth_input_enabled",
        "adaptation_enabled",
        "central_ui_enabled",
        "growth_rate_source",
    }
    expected = {
        item["python"]: item["default"]
        for item in manifest["parameters"]
        if item["owner"] in {"controller", "shared"}
        and "default" in item
        and item["python"] not in runtime_only
    }
    missing = sorted(set(expected) - set(defaults))
    assert missing == []
    for key, value in expected.items():
        if isinstance(value, float):
            assert defaults[key] == pytest.approx(value, rel=1e-15, abs=0.0), key
        else:
            assert defaults[key] == value, key
