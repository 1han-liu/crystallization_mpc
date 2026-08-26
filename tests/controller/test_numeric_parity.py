from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.io import loadmat

from crystallization_mpc.apps.controller.translated.adaptation import (
    adapt_growth_parameters,
)
from crystallization_mpc.apps.controller.translated.control import (
    calc_T_j,
    calc_T_j_set,
    calc_T_with_zero_dT_dt,
    calc_dT_dt_model,
    calc_dT_dt_set,
    calc_dT_j_dt,
    calc_e_dT_dt,
    calc_e_target,
    calc_t_lag_perc,
    calc_t_lag_perc2,
    objective_function,
    update_T_j,
)
from crystallization_mpc.apps.controller.translated.dynamics import (
    state_transition_function,
    state_transition_function_T,
    state_transition_matrices,
    state_transition_ode,
)
from crystallization_mpc.apps.controller.translated.ekf import (
    calc_Q,
    calc_R,
    construct_EKF,
    create_EKF_general,
    measurement_function,
    measurement_matrices,
    smooth_EKF_general,
)
from crystallization_mpc.apps.controller.translated.mass_balance import (
    calc_next_crystallization_mass_balance,
    calc_size_from_volume,
    calc_surface_area,
    calc_volume,
)
from crystallization_mpc.apps.controller.translated.parameters import build_parameters
from crystallization_mpc.apps.controller.translated.thermodynamics import (
    calc_G,
    calc_c_meta,
    calc_c_sat,
    calc_dc_sat_dT,
    calc_relative_sigma,
    calc_sigma,
)


FIXTURE = Path(__file__).parent / "fixtures/matlab_r2021a_golden.mat"


@pytest.fixture(scope="module")
def golden() -> dict:
    return loadmat(FIXTURE, simplify_cells=True)


def test_algebra_and_matrix_functions_match_r2021a(golden: dict) -> None:
    params = build_parameters({})
    inputs = golden["inputs"]
    expected = golden["algebra"]
    T, c, x, dt = inputs["T"], inputs["c"], inputs["x"], inputs["dt"]
    sigma, sigma_c_sat = calc_sigma(c, T)
    C, Du = measurement_matrices(params, x)
    A, Bu = state_transition_matrices(params, x, dt)
    dx_dt, ode_A, ode_Bu = state_transition_ode(params, x, dt)
    actual = {
        "c_sat": calc_c_sat(T),
        "c_meta": calc_c_meta(T),
        "dc_sat_dT": calc_dc_sat_dT(T),
        "relative_sigma": calc_relative_sigma(c, T),
        "sigma": sigma,
        "sigma_c_sat": sigma_c_sat,
        "G_control": calc_G(params, c, T),
        "G_process": calc_G(params, c, T, True),
        "volume": calc_volume(inputs["size_list"]),
        "surface_area": calc_surface_area(inputs["size_list"]),
        "size_roundtrip": calc_size_from_volume(calc_volume(inputs["size_list"])),
        "Q": calc_Q(params, dt),
        "R": calc_R(params),
        "C": C,
        "Du": Du,
        "measurement": measurement_function(params, x),
        "A": A,
        "Bu": Bu,
        "dx_dt": dx_dt,
        "ode_A": ode_A,
        "ode_Bu": ode_Bu,
    }
    for name, value in actual.items():
        np.testing.assert_allclose(value, expected[name], rtol=1e-10, atol=1e-12)


def test_dynamics_and_mass_balance_match_r2021a(golden: dict) -> None:
    params = build_parameters({})
    inputs = golden["inputs"]
    expected = golden["algebra"]
    np.testing.assert_allclose(
        state_transition_function(params, inputs["x"], inputs["dt"]),
        expected["x_next"],
        rtol=1e-6,
        atol=1e-10,
    )
    assert state_transition_function_T(
        params, inputs["T"], inputs["T_j"], inputs["dt"]
    ) == pytest.approx(expected["T_next"], abs=1e-5)
    concentration, sizes = calc_next_crystallization_mass_balance(
        params,
        params["m_solvent"] * inputs["c"],
        params["m_solvent"],
        inputs["size_list"],
        inputs["T"],
        inputs["dt"],
    )
    assert concentration == pytest.approx(expected["mass_balance_c"], abs=1e-10)
    np.testing.assert_allclose(sizes, expected["mass_balance_sizes"], rtol=1e-10, atol=1e-12)


def test_control_helpers_match_r2021a(golden: dict) -> None:
    params = build_parameters({})
    inputs = golden["inputs"]
    expected = golden["algebra"]
    actual = {
        "dT_dt_model": calc_dT_dt_model(params, inputs["x"], inputs["T_j"]),
        "T_j_calc": calc_T_j(params, inputs["x"], inputs["dT_dt_set"]),
        "T_zero": calc_T_with_zero_dT_dt(params, inputs["T_j"]),
        "e_dT_dt": calc_e_dT_dt(
            params, inputs["x"], inputs["T_j"], inputs["dT_dt_set"]
        ),
        "e_sigma": calc_e_target(
            params, "sigma", inputs["target_sigma"], inputs["c"], inputs["T"]
        ),
        "e_G": calc_e_target(
            params, "G", inputs["target_G"], inputs["c"], inputs["T"]
        ),
        "objective_sigma": objective_function(
            params,
            inputs["x"],
            "sigma",
            inputs["dT_dt_set"],
            inputs["target_sigma"],
            inputs["dt"],
        ),
        "objective_G": objective_function(
            params,
            inputs["x"],
            "G",
            inputs["dT_dt_set"],
            inputs["target_G"],
            inputs["dt"],
        ),
        "lag_perc": calc_t_lag_perc(
            params["t_lag_perc"],
            params["t_lag_threshold_perc_sigma"],
            params["sigma_set"],
            abs(expected["e_sigma"]),
        ),
        "lag_perc2": calc_t_lag_perc2(0.2, 0.9, 2.0),
        "dT_j_dt_low": calc_dT_j_dt(
            260, params["dT_j_dt_max"], params["T_j_min"], params["T_j_max"]
        ),
        "dT_j_dt_middle": calc_dT_j_dt(
            300, params["dT_j_dt_max"], params["T_j_min"], params["T_j_max"]
        ),
        "dT_j_dt_high": calc_dT_j_dt(
            400, params["dT_j_dt_max"], params["T_j_min"], params["T_j_max"]
        ),
        "updated_T_j": update_T_j(
            300,
            310,
            params["dT_j_dt_max"],
            params["dt"],
            params["dT_j"],
            params["dt_update"],
            params["T_j_min"],
            params["T_j_max"],
        ),
    }
    for name, value in actual.items():
        assert value == pytest.approx(expected[name], rel=1e-10, abs=1e-12)


def test_mpc_pi_sigma_g_outputs_match_r2021a(golden: dict) -> None:
    inputs = golden["inputs"]
    for expected in golden["mode_target"]:
        params = build_parameters({"mode": expected["mode"], "target": expected["target"]})
        dT_dt_set, integral = calc_dT_dt_set(
            params,
            inputs["x"],
            expected["mode"],
            expected["target"],
            params["target_set"],
            params["dT_dt_min"],
            params["dT_dt_max"],
            params["dt"],
            0.25,
        )
        T_j_set, integral_dT, integral_T = calc_T_j_set(
            params,
            inputs["x"],
            dT_dt_set,
            -0.1,
            0.2,
            inputs["T_j"],
            params["T_j_min"],
            params["T_j_max"],
            params["dt"],
            expected["target"] == "G",
        )
        assert dT_dt_set == pytest.approx(expected["dT_dt_set"], rel=1e-5, abs=1e-9)
        assert integral == pytest.approx(expected["int_e_target_dt"], abs=1e-12)
        assert T_j_set == pytest.approx(expected["T_j_set"], abs=1e-5)
        assert integral_dT == pytest.approx(expected["int_e_dT_dt_dt"], abs=1e-10)
        assert integral_T == pytest.approx(expected["int_e_T_dt"], abs=1e-10)


def test_ekf_trajectories_match_r2021a(golden: dict) -> None:
    params = build_parameters({})
    inputs = golden["inputs"]
    expected = golden["ekf"]
    filter_ = construct_EKF(
        np.array([inputs["T"], 0.0, inputs["c"], 0.0]), params, params["dt"]
    )
    states, covariances = [], []
    for measurement in expected["measurements"].T:
        filter_.predict()
        states.append(filter_.correct(measurement))
        covariances.append(filter_.covariance.copy())
    np.testing.assert_allclose(np.asarray(states).T, expected["states"], rtol=1e-6, atol=2e-8)
    np.testing.assert_allclose(
        np.moveaxis(np.asarray(covariances), 0, 2),
        expected["covariances"],
        rtol=1e-6,
        atol=6e-8,
    )

    general = golden["general_ekf"]
    filter_general = create_EKF_general(
        params["dt"], params["q2_G_measure"], params["r_diag_G_measure"], np.zeros(3)
    )
    values = []
    for index in range(len(general["values"])):
        values.append(
            smooth_EKF_general(
                general["values"][: index + 1], filter_general, params["dt"]
            )
        )
    np.testing.assert_allclose(values, general["filtered"], rtol=1e-10, atol=1e-18)


def test_all_seven_adaptation_modes_match_r2021a(golden: dict) -> None:
    params = build_parameters({})
    sigma = np.linspace(0.025, 0.065, 45)
    temperature = np.linspace(303.15, 313.15, 45)
    growth = (
        params["k_0"]
        * 1.15
        * sigma**1.62
        * np.exp(-132000.0 / params["R"] / temperature)
    )
    growth[[1, 4]] = np.nan
    growth[7] = -1.0
    for expected in golden["adaptation"]:
        updated, count = adapt_growth_parameters(
            params,
            growth,
            sigma,
            temperature,
            np.ones(45, dtype=bool),
            max_num_adapt=40,
            min_num_adapt=30,
            adaptive_mode=expected["mode"],
        )
        assert count == expected["num_adapt"] == 40
        assert updated["E_A"] == pytest.approx(expected["E_A"], rel=1e-5)
        assert updated["k_0"] == pytest.approx(expected["k_0"], rel=1e-5)
        assert updated["n"] == pytest.approx(expected["n"], rel=1e-5)


def test_eight_tick_simulation_state_and_seed_replay_match_r2021a(golden: dict) -> None:
    params = build_parameters({"target": "sigma"})
    inputs = golden["inputs"]
    expected = golden["simulation_replay"]
    T = float(params["T_init"])
    T_j = float(params["T_j_init"])
    c = float(params["c_init"])
    sizes = np.zeros_like(inputs["size_list"])
    actual_T, actual_T_j, actual_c = [], [], []
    for index in range(8):
        if index + 1 == expected["seed_added_at_tick"]:
            sizes = np.asarray(inputs["size_list"], dtype=float)
        actual_T.append(T)
        actual_T_j.append(T_j)
        actual_c.append(c)
        if index < 7:
            T = state_transition_function_T(params, T, T_j, params["dt"])
            from crystallization_mpc.apps.controller.translated.control import update_T_j

            T_j = update_T_j(
                expected["T_j_set"][index],
                T_j,
                params["dT_j_dt_max"],
                params["dt"],
                params["dT_j"],
                params["dt_update"],
                params["T_j_min"],
                params["T_j_max"],
            )
            c, sizes = calc_next_crystallization_mass_balance(
                params,
                params["m_solvent"] * c,
                params["m_solvent"],
                sizes,
                actual_T[-1],
                params["dt"],
            )
    np.testing.assert_allclose(actual_T, expected["T"], rtol=1e-6, atol=1e-5)
    np.testing.assert_allclose(actual_T_j, expected["T_j"], rtol=1e-10, atol=1e-10)
    np.testing.assert_allclose(actual_c, expected["c"], rtol=1e-10, atol=1e-10)


def test_golden_scenario_matrix_contains_only_allowed_v1_combinations(golden: dict) -> None:
    combinations = {
        (row["run_type"], row["growth_rate_source"])
        for row in golden["scenario_matrix"]
    }
    assert combinations == {
        ("experiment", "live_gsensor"),
        ("simulation", "simulated"),
        ("simulation", "live_gsensor"),
        ("simulation", "presaved_images"),
    }
