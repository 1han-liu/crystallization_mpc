import numpy as np
import pytest

from crystallization_mpc.apps.gsensor.morphs.calc_angle_d import calc_angle_d


@pytest.mark.parametrize(
    ("line1", "line2", "expected"),
    [
        ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], 90.0),
        ([1.0, 0.0, 0.0], [1.0, 0.0, 0.0], 0.0),
        ([1.0, 0.0, 0.0], [-1.0, 0.0, 0.0], 180.0),
        ([1.0, 1.0, 0.0], [1.0, 0.0, 0.0], 45.0),
    ],
)
def test_calc_angle_d_returns_degrees_between_vectors(line1, line2, expected):
    assert calc_angle_d(line1, line2) == pytest.approx(expected)


def test_calc_angle_d_keeps_matlab_zero_norm_nan_semantics():
    with np.errstate(invalid="ignore", divide="ignore"):
        assert np.isnan(calc_angle_d([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]))
