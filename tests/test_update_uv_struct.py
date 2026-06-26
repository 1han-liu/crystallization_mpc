from types import SimpleNamespace

import crystallization_mpc.apps.gsensor.detection.update_uv_struct as module


def test_update_uv_struct_updates_line_dist_and_distance_array(monkeypatch):
    image = object()
    calls = []
    uv_struct = SimpleNamespace(
        line="old_line",
        t="t",
        e="e",
        n="n",
        o="o",
        is_opposite=True,
        dist_array=[1.0],
    )

    def fake_update_line(image_file, params_G, line, t, e, n, o, is_opposite, kernel):
        calls.append((image_file, params_G, line, t, e, n, o, is_opposite, kernel))
        return "new_line", 12.5, image

    monkeypatch.setattr(module, "update_line", fake_update_line)

    result, I_orig = module.update_uv_struct(
        uv_struct,
        "frame.png",
        2,
        "params",
        "kernel",
    )

    assert result is uv_struct
    assert I_orig is image
    assert uv_struct.line == "new_line"
    assert uv_struct.dist == 12.5
    assert uv_struct.dist_array == [1.0, 12.5]
    assert calls == [
        ("frame.png", "params", "old_line", "t", "e", "n", "o", True, "kernel")
    ]


def test_update_uv_struct_creates_distance_array(monkeypatch):
    uv_struct = SimpleNamespace(
        line="old_line",
        t="t",
        e="e",
        n="n",
        o="o",
        is_opposite=False,
    )

    monkeypatch.setattr(
        module,
        "update_line",
        lambda *args: ("line", 3.0, "image"),
    )

    module.update_uv_struct(uv_struct, "frame.png", 1, "params", "kernel")

    assert uv_struct.dist_array == [3.0]
