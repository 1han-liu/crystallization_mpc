from __future__ import annotations

import pytest
import yaml

from crystallization_mpc.apps.central.params import apply_derived_params
from crystallization_mpc.apps.controller.translated.parameters import build_parameters


def _sections() -> tuple[dict[str, object], dict[str, object]]:
    with open("params_default.yaml", encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    shared = {item["key"]: item["default"] for item in document["params"]["shared"]}
    controller = {
        item["key"]: item["default"] for item in document["params"]["controller"]
    }
    return shared, controller


@pytest.mark.parametrize("target", ["sigma", "G"])
def test_central_derivations_equal_controller_contract(target: str) -> None:
    shared, controller = _sections()
    _shared, central, _derived = apply_derived_params(shared, controller, target=target)
    translated = build_parameters({**shared, **central, "target": target})
    for central_key, translated_key in {
        "area_1": "area_1",
        "area_2": "area_2",
        "params.tau_1": "tau_1",
        "params.tau_2": "tau_2",
        "params.rho_solute": "rho_solute",
        "params.k_0": "k_0",
        "params.k_0_proc": "k_0_proc",
        "params.K_P_target": "K_P_target",
        "params.K_I_target": "K_I_target",
        "target_set": "target_set",
        "params.K_P_T": "K_P_T",
        "params.K_I_T": "K_I_T",
        "dT_dt_min": "dT_dt_min",
        "dT_dt_max": "dT_dt_max",
        "t_lag_threshold_perc": "t_lag_threshold_perc",
        "T_init": "T_init",
        "steps": "steps",
        "seed_time": "seed_time",
    }.items():
        assert central[central_key] == pytest.approx(translated[translated_key], rel=1e-14)


@pytest.mark.parametrize(
    "params",
    [
        {"run_type": "experiment", "growth_rate_source": "simulated"},
        {"run_type": "experiment", "growth_rate_source": "presaved_images"},
    ],
)
def test_v1_rejects_non_live_growth_source_for_experiment(params: dict) -> None:
    with pytest.raises(ValueError, match="requires live_gsensor"):
        build_parameters(params)
