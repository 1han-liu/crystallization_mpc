import pytest

from crystallization_mpc.apps.gsensor.morphs.get_angles_d import get_angles_d


@pytest.mark.parametrize(
    ("corner", "expected"),
    [
        ("A", (61.60038904, 82.59533203, 112.0661066)),
        ("B", (38.2970794, 69.71231051, 84.1233756)),
        ("C", (53.22106313, 59.96173898, 70.20060129)),
    ],
)
def test_get_angles_d_returns_corner_angle_group(corner, expected):
    assert get_angles_d(corner) == pytest.approx(expected)


def test_get_angles_d_rejects_unknown_corner():
    with pytest.raises(KeyError):
        get_angles_d("D")
