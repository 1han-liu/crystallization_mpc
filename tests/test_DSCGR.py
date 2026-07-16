import csv
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import crystallization_mpc.apps.gsensor.DSCGR as module
import crystallization_mpc.apps.gsensor.app as app_module


def _touch(path: Path, timestamp: float) -> None:
    path.write_bytes(b"image")
    os.utime(path, (timestamp, timestamp))


def _set_list_value(target, attr: str, ii: int, value: float) -> None:
    values = getattr(target, attr, [])
    index = ii - 1
    if len(values) <= index:
        values.extend([None] * (index + 1 - len(values)))
    values[index] = value
    setattr(target, attr, values)


def test_DSCGR_writes_json_csv_and_overlay_images_without_mat(monkeypatch, tmp_path):
    image_folder = tmp_path / "images"
    image_folder.mkdir()
    for index in range(1, 5):
        _touch(image_folder / f"IMG_{index:03d}.png", float(index))

    output_dir = tmp_path / "imgs"
    params = {
        "dt_G": 15,
        "resolution": 8.676e-7,
        "q2": 1e-16,
        "r_diag": [4, 3, 2],
        "params_G.width": 100,
        "params_G.ratio": 10,
        "params_G.delta_theta": 10,
        "params_G.width_divider": 4,
        "params_G.num_peak": 10,
        "params_G.len_min": 50,
    }
    uv_structs = [SimpleNamespace(name="u"), SimpleNamespace(name="v")]
    seen_ptrs = []
    seen_params_G = []

    def fake_initialize_uv_struct(uv_struct, dt_G, resolution, q2, r_diag):
        uv_struct.line = SimpleNamespace(
            point1=np.array([0.0, 0.0]),
            point2=np.array([1.0, 1.0]),
        )
        return uv_struct

    def fake_update_uv_struct(uv_struct, image_file, ii, params_G, kernel):
        seen_ptrs.append(int(Path(image_file).stem.split("_")[1]))
        seen_params_G.append(params_G)
        return uv_struct, np.zeros((8, 8, 3), dtype=np.uint8)

    def fake_update_EKF_G(uv_struct, dt_G, resolution, ii):
        base = 10.0 if uv_struct.name == "u" else 20.0
        _set_list_value(uv_struct, "distance_array", ii, base + ii)
        _set_list_value(uv_struct, "distance_KF_array", ii, base + ii + 0.1)
        _set_list_value(uv_struct, "G_array", ii, base + ii + 0.2)
        _set_list_value(uv_struct, "G_KF_array", ii, base + ii + 0.3)
        return uv_struct

    def fake_update_figure(u_struct, v_struct, image, file, output_dir=None):
        overlay_path = Path(output_dir) / f"{Path(file).stem}.jpg"
        overlay_path.write_bytes(b"overlay")
        return overlay_path

    monkeypatch.setattr(module, "initialize_uv_struct", fake_initialize_uv_struct)
    monkeypatch.setattr(module, "update_uv_struct", fake_update_uv_struct)
    monkeypatch.setattr(module, "update_EKF_G", fake_update_EKF_G)
    monkeypatch.setattr(module, "update_figure", fake_update_figure)

    log_lines = []
    result = module.DSCGR(
        image_folder,
        params,
        output_dir=output_dir,
        uv_struct_list=uv_structs,
        kernel="kernel",
        print_fn=log_lines.append,
    )

    assert result["processed_ptrs"] == [2, 3, 4]
    assert seen_ptrs == [2, 2, 3, 3, 4, 4]
    assert all(params_G.width == 100 for params_G in seen_params_G)
    assert all(not hasattr(params_G, "params_G.width") for params_G in seen_params_G)
    assert not list(output_dir.rglob("*.mat"))

    json_path = output_dir / "DSCGR_data.json"
    csv_path = output_dir / "DSCGR_data.csv"
    assert result["json_path"] == str(json_path)
    assert result["csv_path"] == str(csv_path)
    assert json_path.exists()
    assert csv_path.exists()
    assert (output_dir / "overlays" / "IMG_002.jpg").exists()
    assert (output_dir / "overlays" / "IMG_003.jpg").exists()
    assert (output_dir / "overlays" / "IMG_004.jpg").exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "offline_dscgr"
    assert payload["processed_ptrs"] == [2, 3, 4]
    assert len(payload["records"]) == 6
    assert payload["records"][0]["edge"] == "u"
    assert payload["records"][0]["G"] == 11.2
    assert payload["records"][1]["edge"] == "v"
    assert payload["records"][1]["G_KF"] == 21.3

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6
    assert rows[0]["ptr"] == "2"
    assert rows[0]["edge"] == "u"
    assert log_lines == [
        "2: u | measured: 11.2, KF: 11.3; v | measured: 21.2, KF: 21.3; ",
        "3: u | measured: 12.2, KF: 12.3; v | measured: 22.2, KF: 22.3; ",
        "4: u | measured: 13.2, KF: 13.3; v | measured: 23.2, KF: 23.3; ",
    ]


def test_DSCGR_stops_after_last_image(monkeypatch, tmp_path):
    image_folder = tmp_path / "images"
    image_folder.mkdir()
    for index in range(1, 4):
        _touch(image_folder / f"IMG_{index:03d}.png", float(index))

    params = {
        "dt_G": 15,
        "resolution": 8.676e-7,
        "q2": 1e-16,
        "r_diag": [4, 3, 2],
        "params_G.width": 100,
        "params_G.ratio": 10,
        "params_G.delta_theta": 10,
        "params_G.width_divider": 4,
        "params_G.num_peak": 10,
        "params_G.len_min": 50,
    }
    uv_structs = [SimpleNamespace(name="u"), SimpleNamespace(name="v")]
    seen_ptrs = []

    def fake_initialize_uv_struct(uv_struct, dt_G, resolution, q2, r_diag):
        return uv_struct

    def fake_update_uv_struct(uv_struct, image_file, ii, params_G, kernel):
        seen_ptrs.append(int(Path(image_file).stem.split("_")[1]))
        return uv_struct, np.zeros((8, 8, 3), dtype=np.uint8)

    def fake_update_EKF_G(uv_struct, dt_G, resolution, ii):
        _set_list_value(uv_struct, "distance_array", ii, 1.0)
        _set_list_value(uv_struct, "distance_KF_array", ii, 1.0)
        _set_list_value(uv_struct, "G_array", ii, 1.0)
        _set_list_value(uv_struct, "G_KF_array", ii, 1.0)
        return uv_struct

    def fake_update_figure(u_struct, v_struct, image, file, output_dir=None):
        overlay_path = Path(output_dir) / f"{Path(file).stem}.jpg"
        overlay_path.write_bytes(b"overlay")
        return overlay_path

    monkeypatch.setattr(module, "initialize_uv_struct", fake_initialize_uv_struct)
    monkeypatch.setattr(module, "update_uv_struct", fake_update_uv_struct)
    monkeypatch.setattr(module, "update_EKF_G", fake_update_EKF_G)
    monkeypatch.setattr(module, "update_figure", fake_update_figure)

    result = module.DSCGR(
        image_folder,
        params,
        output_dir=tmp_path / "imgs",
        uv_struct_list=uv_structs,
        kernel="kernel",
        print_fn=None,
    )

    assert result["processed_ptrs"] == [2, 3]
    assert seen_ptrs == [2, 2, 3, 3]


def test_gsensor_service_runs_DSCGR_from_ready_initialization(monkeypatch, tmp_path):
    gsensor_service = app_module.GsensorService(dscgr_output_root_path=tmp_path)
    gsensor_service.initialization = SimpleNamespace(
        payload=lambda session_id=None: {
            "session_id": session_id or "active-session",
            "status": "ready_for_3d",
            "image_folder": str(tmp_path / "images"),
        }
    )
    monkeypatch.setattr(gsensor_service, "current_params", lambda: {"dt_G": 15})
    calls = {}

    def fake_initialize_DSCGR(initialization, session_id=None):
        calls["initialize"] = (initialization, session_id)
        return ["u_struct", "v_struct"], "kernel"

    def fake_DSCGR(folder_G, params, uv_struct_list, kernel, output_dir="imgs"):
        calls["dscgr"] = {
            "folder_G": folder_G,
            "params": params,
            "uv_struct_list": uv_struct_list,
            "kernel": kernel,
            "output_dir": Path(output_dir),
        }
        return {
            "processed_ptrs": [2],
            "records": [],
            "output_dir": str(output_dir),
        }

    monkeypatch.setattr(app_module, "initialize_DSCGR", fake_initialize_DSCGR)
    monkeypatch.setattr(app_module, "DSCGR", fake_DSCGR)

    result = gsensor_service.run_dscgr("session-1")

    assert result["processed_ptrs"] == [2]
    assert calls["initialize"][1:] == ("session-1",)
    assert calls["dscgr"]["folder_G"] == str(tmp_path / "images")
    assert calls["dscgr"]["params"] == {"dt_G": 15}
    assert calls["dscgr"]["uv_struct_list"] == ["u_struct", "v_struct"]
    assert calls["dscgr"]["kernel"] == "kernel"
    assert calls["dscgr"]["output_dir"].parent == tmp_path
    assert gsensor_service.last_dscgr_result is result


def test_gsensor_service_rejects_DSCGR_before_initialization_ready(tmp_path):
    gsensor_service = app_module.GsensorService(dscgr_output_root_path=tmp_path)
    gsensor_service.initialization = SimpleNamespace(
        payload=lambda session_id=None: {"status": "ready_for_3d_choice"}
    )

    with pytest.raises(ValueError, match="Complete initialization"):
        gsensor_service.run_dscgr("session-1")
