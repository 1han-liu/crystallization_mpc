from types import SimpleNamespace

import numpy as np
from PIL import Image

from crystallization_mpc.apps.gsensor.detection.update_figure import update_figure


def _struct(t, e, p1, p2):
    return SimpleNamespace(
        t=np.asarray(t, dtype=float),
        e=np.asarray(e, dtype=float),
        line=SimpleNamespace(
            point1=np.asarray(p1, dtype=float),
            point2=np.asarray(p2, dtype=float),
        ),
    )


def test_update_figure_saves_local_diagnostic_image(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)
    image = np.zeros((24, 24, 3), dtype=np.uint8)
    u_struct = _struct([2, 2, 0], [20, 2, 0], [2, 6, 0], [20, 6, 0])
    v_struct = _struct([2, 12, 0], [20, 12, 0], [2, 16, 0], [20, 16, 0])

    output_path = update_figure(u_struct, v_struct, image, SimpleNamespace(name="frame.PNG"))

    assert output_path.resolve() == tmp_path / "gsensor_data" / "images" / "frame.jpg"
    assert output_path.exists()
    saved = np.asarray(Image.open(output_path))
    assert saved.shape[:2] == (24, 24)
    assert np.max(saved) > 0


def test_update_figure_uses_matlab_uppercase_png_replacement(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)
    image = np.zeros((8, 8), dtype=np.uint8)
    u_struct = _struct([1, 1], [6, 1], [1, 2], [6, 2])
    v_struct = _struct([1, 4], [6, 4], [1, 5], [6, 5])

    output_path = update_figure(u_struct, v_struct, image, {"name": "frame.png"})

    assert output_path.name == "frame.png"
    assert output_path.exists()


def test_update_figure_accepts_output_dir(tmp_path):
    image = np.zeros((8, 8), dtype=np.uint8)
    u_struct = _struct([1, 1], [6, 1], [1, 2], [6, 2])
    v_struct = _struct([1, 4], [6, 4], [1, 5], [6, 5])
    output_dir = tmp_path / "overlays"

    output_path = update_figure(
        u_struct,
        v_struct,
        image,
        SimpleNamespace(name="frame.PNG"),
        output_dir=output_dir,
    )

    assert output_path == output_dir / "frame.jpg"
    assert output_path.exists()
