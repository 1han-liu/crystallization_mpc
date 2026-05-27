import numpy as np

import crystallization_mpc.apps.gsensor.morphs.recover_3d_all as recover_3d_all_module
from crystallization_mpc.apps.gsensor.morphs.recover_3d_all import (
    NON_FULL_DIRECTIONS,
    recover_3d_all,
)


def _fake_recover_3d(m, w, u, v, corner, is_full, direction):
    offset = float(len(direction))

    def point_3d(point, z):
        point = np.asarray(point, dtype=float).reshape(-1)
        return np.array([point[0], point[1], z])

    return (
        point_3d(m, 0.0),
        point_3d(w, offset),
        point_3d(u, offset + 1.0),
        point_3d(v, offset + 2.0),
    )


def test_recover_3d_all_full_mode_returns_outwards_choice(monkeypatch):
    monkeypatch.setattr(recover_3d_all_module, "recover_3d", _fake_recover_3d)

    candidates = recover_3d_all(
        None,
        [0.0, 0.0],
        [1.0, 0.0],
        [0.0, 1.0],
        [1.0, 1.0],
        "A",
        True,
    )

    assert len(candidates) == 1
    assert candidates[0].choice == 2
    assert candidates[0].direction == "outwards"
    assert candidates[0].label == "Corner points outwards"


def test_recover_3d_all_non_full_mode_preserves_direction_order(monkeypatch):
    monkeypatch.setattr(recover_3d_all_module, "recover_3d", _fake_recover_3d)

    candidates = recover_3d_all(
        None,
        [0.0, 0.0],
        [1.0, 0.0],
        [0.0, 1.0],
        [1.0, 1.0],
        "B",
        False,
    )

    assert len(candidates) == 16
    assert [candidate.choice for candidate in candidates] == list(range(1, 17))
    assert [candidate.direction for candidate in candidates] == list(NON_FULL_DIRECTIONS)


def test_recover_3d_all_candidates_contain_3d_points(monkeypatch):
    monkeypatch.setattr(recover_3d_all_module, "recover_3d", _fake_recover_3d)

    candidate = recover_3d_all(
        None,
        [0.0, 0.0],
        [1.0, 0.0],
        [0.0, 1.0],
        [1.0, 1.0],
        "C",
        False,
    )[0]

    payload = candidate.as_dict()
    for key in ("M", "W", "U", "V"):
        assert len(payload[key]) == 3
        assert np.all(np.isfinite(payload[key]))
    assert payload["show_3d"]["vertices"][1][2] == -payload["W"][2]
    assert payload["show_3d"]["faces"] == [[1, 2, 3], [1, 2, 4], [1, 3, 4], [2, 3, 4]]
