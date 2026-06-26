from types import SimpleNamespace

import numpy as np

import crystallization_mpc.apps.gsensor.detection.update_line as module


def _params():
    return SimpleNamespace(
        width=8.0,
        ratio=0.0,
        delta_theta=2.0,
        width_divider=2.0,
        num_peak=3,
        len_min=5.0,
    )


def _line():
    return SimpleNamespace(
        point1=np.array([10.0, 2.0, 0.0]),
        point2=np.array([10.0, 28.0, 0.0]),
        theta=0.0,
        rho=10.0,
    )


def test_update_line_selects_hough_candidate(monkeypatch):
    original_image = np.zeros((32, 32, 3), dtype=np.uint8)
    edge_image = np.zeros((32, 32), dtype=bool)
    edge_image[4:28, 9] = True
    calls = []

    monkeypatch.setattr(module, "imread", lambda path: original_image)

    def fake_find_edge_points_yolov(I, kernel):
        calls.append((I, kernel))
        return edge_image

    monkeypatch.setattr(module, "find_edge_points_yolov", fake_find_edge_points_yolov)

    line, dist, I_orig = module.update_line(
        SimpleNamespace(folder="imgs", name="frame.png"),
        _params(),
        _line(),
        np.array([10.0, 1.0, 0.0]),
        np.array([10.0, 30.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        np.array([12.0, 1.0, 0.0]),
        True,
        "kernel",
    )

    assert calls == [(original_image, "kernel")]
    assert I_orig is original_image
    np.testing.assert_allclose(line.point1[0], 10.0)
    np.testing.assert_allclose(line.point2[0], 10.0)
    assert np.linalg.norm(line.point1 - line.point2) >= 5.0
    assert dist == 0.0


def test_update_line_keeps_old_line_when_no_candidate(monkeypatch):
    original_image = np.zeros((16, 16, 3), dtype=np.uint8)
    old_line = _line()

    monkeypatch.setattr(module, "imread", lambda path: original_image)
    monkeypatch.setattr(
        module,
        "find_edge_points_yolov",
        lambda I, kernel: np.zeros((16, 16), dtype=bool),
    )

    line, dist, I_orig = module.update_line(
        {"folder": "imgs", "name": "frame.png"},
        _params(),
        old_line,
        np.array([10.0, 1.0, 0.0]),
        np.array([10.0, 30.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        np.array([12.0, 1.0, 0.0]),
        False,
        None,
    )

    assert line is old_line
    assert I_orig is original_image
    assert dist == 0.0


def test_houghpeaks_zeroes_neighborhood():
    H = np.zeros((9, 3))
    H[4, 1] = 10
    H[5, 1] = 9
    H[0, 0] = 8

    peaks = module.houghpeaks(H, 2, nhood_size=(3, 1))

    assert peaks == [(4, 1), (0, 0)]
