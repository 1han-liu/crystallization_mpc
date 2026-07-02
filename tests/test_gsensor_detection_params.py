import pytest

from crystallization_mpc.apps.gsensor.detection.params import (
    PARAMS_G_KEYS,
    build_params_G,
)


def test_build_params_G_strips_runtime_prefix_for_detection():
    params = {
        "dt_G": 15,
        "resolution": 8.676e-7,
        "params_G.width": 100,
        "params_G.ratio": 10,
        "params_G.delta_theta": 8,
        "params_G.width_divider": 4,
        "params_G.num_peak": 12,
        "params_G.len_min": 50,
    }

    params_G = build_params_G(params)

    assert set(vars(params_G)) == set(PARAMS_G_KEYS)
    assert params_G.width == 100
    assert params_G.ratio == 10
    assert params_G.delta_theta == 8
    assert params_G.width_divider == 4
    assert params_G.num_peak == 12
    assert params_G.len_min == 50


def test_build_params_G_requires_flat_runtime_keys():
    with pytest.raises(KeyError, match="params_G.width"):
        build_params_G({"width": 100})
