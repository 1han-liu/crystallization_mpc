import base64
from pathlib import Path
import shutil
from uuid import uuid4

import pytest
from fastapi import HTTPException

from crystallization_mpc.apps.gsensor.app import (
    GsensorService,
    Initialization3DChoiceRequest,
    InitializationResetRequest,
    InitializationUploadFolderRequest,
    InitializationUploadedFile,
    choose_initialization_3d_choice,
    get_initialization_step,
    reset_initialization,
    service,
    upload_initialization_folder,
    web_app,
)
import crystallization_mpc.apps.gsensor.initialization as initialization_module
from crystallization_mpc.apps.gsensor.initialization import GsensorInitializationManager
from crystallization_mpc.apps.gsensor.morphs.recover_3d_all import Recover3DCandidate


def _write_png_header(path: Path, width: int = 64, height: int = 48) -> None:
    path.write_bytes(_png_header_bytes(width, height))


def _png_header_bytes(width: int = 64, height: int = 48) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def _uploaded_png_files() -> list[InitializationUploadedFile]:
    return [
        InitializationUploadedFile(
            filename="selected/00002.png",
            content_base64=base64.b64encode(_png_header_bytes(80, 60)).decode("ascii"),
        ),
        InitializationUploadedFile(
            filename="selected/00001.png",
            content_base64=base64.b64encode(_png_header_bytes(64, 48)).decode("ascii"),
        ),
    ]


def _make_image_folder() -> Path:
    folder = Path(__file__).resolve().parent / ".tmp_gsensor_initialization" / uuid4().hex / "images"
    folder.mkdir(parents=True)
    _write_png_header(folder / "00002.png", width=80, height=60)
    _write_png_header(folder / "00001.png", width=64, height=48)
    return folder


def _cleanup_image_folder(folder: Path) -> None:
    shutil.rmtree(folder.parent, ignore_errors=True)


def _submit(manager: GsensorInitializationManager, session_id: str, key: str, x: float, y: float):
    payload = manager.payload(session_id)
    assert payload["current_step"]["key"] == key
    return manager.submit_point(session_id, x, y)


def _submit_adjacent_sides(manager: GsensorInitializationManager, session_id: str):
    _submit(manager, session_id, "u_ad.t", 10, 10)
    _submit(manager, session_id, "u_ad.e", 20, 10)
    _submit(manager, session_id, "u_ad.o", 15, 5)
    _submit(manager, session_id, "v_ad.t", 5, 15)
    _submit(manager, session_id, "v_ad.e", 5, 25)
    return _submit(manager, session_id, "v_ad.o", 1, 20)


def _submit_kernel(manager: GsensorInitializationManager, session_id: str):
    for index, point in enumerate(((1, 1), (2, 1), (2, 2), (1, 2)), start=1):
        _submit(manager, session_id, f"kernel.k_c_cell.{index}", point[0], point[1])
        _submit(manager, session_id, f"kernel.k_o_cell.{index}", point[0] + 10, point[1] + 10)


def _patch_recover_3d_all(monkeypatch):
    def fake_recover_3d_all(_image, m, w, u, v, corner, is_full):
        del m, w, u, v, corner
        if is_full:
            return [
                Recover3DCandidate(
                    choice=2,
                    direction="outwards",
                    label="Corner points outwards",
                    M=[0.0, 0.0, 0.0],
                    W=[1.0, 0.0, 2.0],
                    U=[0.0, 1.0, 3.0],
                    V=[1.0, 1.0, 4.0],
                )
            ]
        return [
            Recover3DCandidate(
                choice=1,
                direction="in-1-1",
                label="Corner points in-1-1",
                M=[0.0, 0.0, 0.0],
                W=[1.0, 0.0, 1.0],
                U=[0.0, 1.0, 2.0],
                V=[1.0, 1.0, 3.0],
            )
        ]

    monkeypatch.setattr(initialization_module, "recover_3d_all", fake_recover_3d_all)


def test_initialization_folder_selects_first_and_latest_images():
    folder = _make_image_folder()
    manager = GsensorInitializationManager()
    try:
        first = manager.start_folder(str(folder), "first")
        latest = manager.start_folder(str(folder), "latest")

        assert first["selected_image"].endswith("00001.png")
        assert first["image_width"] == 64
        assert first["image_height"] == 48
        assert latest["selected_image"].endswith("00002.png")
    finally:
        _cleanup_image_folder(folder)


def test_uploaded_initialization_folder_uses_selected_local_images(tmp_path):
    gsensor_service = GsensorService(upload_root_path=tmp_path)
    payload = gsensor_service.start_uploaded_initialization(
        [
            *_uploaded_png_files(),
            InitializationUploadedFile(
                filename="selected/readme.txt",
                content_base64=base64.b64encode(b"not an image").decode("ascii"),
            ),
        ],
        image_choice="first",
    )

    assert payload["selected_image"].endswith("00001.png")
    assert payload["image_width"] == 64
    assert payload["image_height"] == 48
    assert Path(payload["image_folder"]).is_dir()


def test_uploaded_initialization_folder_rejects_folder_without_images(tmp_path):
    gsensor_service = GsensorService(upload_root_path=tmp_path)

    with pytest.raises(ValueError):
        gsensor_service.start_uploaded_initialization(
            [
                InitializationUploadedFile(
                    filename="selected/readme.txt",
                    content_base64=base64.b64encode(b"not an image").decode("ascii"),
                )
            ]
        )


def test_server_folder_initialization_route_is_not_registered():
    route_paths = {getattr(route, "path", None) for route in web_app.routes}

    assert "/api/initialization/folder" not in route_paths
    assert "/api/initialization/upload-folder" in route_paths


def test_non_full_initialization_steps_compute_m_u_v_and_kernel():
    folder = _make_image_folder()
    manager = GsensorInitializationManager()
    try:
        payload = manager.start_folder(str(folder))
        session_id = payload["session_id"]

        payload = manager.set_is_full(session_id, False)
        assert payload["current_step"]["key"] == "u_ad.t"

        payload = _submit_adjacent_sides(manager, session_id)
        assert payload["derived"]["m"][:2] == pytest.approx([5, 10])
        assert payload["derived"]["u"][:2] == pytest.approx([25, 10])
        assert payload["derived"]["v"][:2] == pytest.approx([5, 30])
        assert payload["current_step"]["key"] == "w"

        _submit(manager, session_id, "w", 30, 30)
        _submit_kernel(manager, session_id)
        payload = manager.payload(session_id)

        assert payload["status"] == "ready_for_corner"
        assert payload["current_step"]["type"] == "corner"
        assert len([key for key in payload["points"] if key.startswith("kernel.k_c_cell")]) == 4
        assert len([key for key in payload["points"] if key.startswith("kernel.k_o_cell")]) == 4
    finally:
        _cleanup_image_folder(folder)


def test_full_initialization_steps_compute_u_v_w_and_select_3d_choice(monkeypatch):
    _patch_recover_3d_all(monkeypatch)
    folder = _make_image_folder()
    manager = GsensorInitializationManager()
    try:
        payload = manager.start_folder(str(folder))
        session_id = payload["session_id"]
        manager.set_is_full(session_id, True)

        _submit_adjacent_sides(manager, session_id)
        _submit(manager, session_id, "u_op.t", 30, 10)
        _submit(manager, session_id, "u_op.e", 30, 40)
        _submit(manager, session_id, "u_op.o", 35, 15)
        _submit(manager, session_id, "v_op.t", 5, 30)
        _submit(manager, session_id, "v_op.e", 30, 30)
        payload = _submit(manager, session_id, "v_op.o", 15, 35)

        assert payload["derived"]["u"][:2] == pytest.approx([30, 10])
        assert payload["derived"]["v"][:2] == pytest.approx([5, 30])
        assert payload["derived"]["w"][:2] == pytest.approx([30, 30])

        _submit_kernel(manager, session_id)
        payload = manager.choose_corner(session_id, "B")

        assert payload["status"] == "ready_for_3d_choice"
        assert payload["corner"] == "B"
        assert payload["current_step"]["type"] == "3d_choice"
        assert len(payload["candidates_3d"]) == 1
        assert payload["candidates_3d"][0]["choice"] == 2

        payload = manager.select_3d_choice(session_id, 2)
        assert payload["status"] == "ready_for_3d"
        assert payload["selected_3d_choice"] == 2
        assert payload["recovered_3d"]["M"] == pytest.approx([0.0, 0.0, 0.0])
        assert payload["current_step"] is None

        reset_payload = manager.reset(session_id)
        assert reset_payload["session_id"] == session_id
        assert reset_payload["selected_image"] == payload["selected_image"]
        assert reset_payload["is_full"] is None
        assert reset_payload["status"] == "awaiting_is_full"
        assert reset_payload["points"] == {}
        assert reset_payload["corner"] is None
        assert reset_payload["candidates_3d"] == []
        assert reset_payload["selected_3d_choice"] is None
        assert reset_payload["recovered_3d"] is None
        assert reset_payload["current_step"]["key"] == "is_full"
    finally:
        _cleanup_image_folder(folder)


def test_initialization_rejects_invalid_3d_choice_without_final_selection(monkeypatch):
    _patch_recover_3d_all(monkeypatch)
    folder = _make_image_folder()
    manager = GsensorInitializationManager()
    try:
        payload = manager.start_folder(str(folder))
        session_id = payload["session_id"]
        manager.set_is_full(session_id, False)

        _submit_adjacent_sides(manager, session_id)
        _submit(manager, session_id, "w", 30, 30)
        _submit_kernel(manager, session_id)
        payload = manager.choose_corner(session_id, "A")
        assert payload["status"] == "ready_for_3d_choice"

        with pytest.raises(ValueError):
            manager.select_3d_choice(session_id, 99)

        payload = manager.payload(session_id)
        assert payload["status"] == "ready_for_3d_choice"
        assert payload["selected_3d_choice"] is None
        assert payload["recovered_3d"] is None
    finally:
        _cleanup_image_folder(folder)


def test_initialization_undo_and_coordinate_validation():
    folder = _make_image_folder()
    manager = GsensorInitializationManager()
    try:
        payload = manager.start_folder(str(folder))
        session_id = payload["session_id"]
        manager.set_is_full(session_id, False)

        _submit(manager, session_id, "u_ad.t", 10, 0)
        payload = manager.undo(session_id)

        assert payload["current_step"]["key"] == "u_ad.t"
        with pytest.raises(ValueError):
            manager.submit_point(session_id, -1, 0)
    finally:
        _cleanup_image_folder(folder)


def test_initialization_rejects_degenerate_side_without_storing_point():
    folder = _make_image_folder()
    manager = GsensorInitializationManager()
    try:
        payload = manager.start_folder(str(folder))
        session_id = payload["session_id"]
        manager.set_is_full(session_id, False)

        _submit(manager, session_id, "u_ad.t", 10, 10)
        with pytest.raises(ValueError):
            manager.submit_point(session_id, 10, 10)

        payload = manager.payload(session_id)
        assert payload["current_step"]["key"] == "u_ad.e"
        assert "u_ad.e" not in payload["points"]
    finally:
        _cleanup_image_folder(folder)


def test_initialization_rejects_parallel_side_lines_without_storing_point():
    folder = _make_image_folder()
    manager = GsensorInitializationManager()
    try:
        payload = manager.start_folder(str(folder))
        session_id = payload["session_id"]
        manager.set_is_full(session_id, False)

        _submit(manager, session_id, "u_ad.t", 10, 10)
        _submit(manager, session_id, "u_ad.e", 20, 10)
        _submit(manager, session_id, "u_ad.o", 15, 5)
        _submit(manager, session_id, "v_ad.t", 5, 15)
        with pytest.raises(ValueError):
            manager.submit_point(session_id, 25, 15)

        payload = manager.payload(session_id)
        assert payload["current_step"]["key"] == "v_ad.e"
        assert "v_ad.e" not in payload["points"]
    finally:
        _cleanup_image_folder(folder)


def test_initialization_api_upload_reset_and_invalid_session(monkeypatch):
    _patch_recover_3d_all(monkeypatch)
    service.initialization.reset()
    uploaded_folder: Path | None = None
    try:
        payload = upload_initialization_folder(
            InitializationUploadFolderRequest(files=_uploaded_png_files(), image_choice="first")
        )
        uploaded_folder = Path(payload["image_folder"])
        session_id = payload["session_id"]

        with pytest.raises(HTTPException) as exc:
            get_initialization_step(session_id="missing")
        assert exc.value.status_code == 400

        selected_image = payload["selected_image"]
        service.initialization.set_is_full(session_id, False)
        _submit(service.initialization, session_id, "u_ad.t", 10, 10)

        payload = reset_initialization(InitializationResetRequest(session_id=session_id))
        assert payload["session_id"] == session_id
        assert payload["selected_image"] == selected_image
        assert payload["status"] == "awaiting_is_full"
        assert payload["is_full"] is None
        assert payload["points"] == {}
        assert payload["can_undo"] is False
        assert payload["current_step"]["key"] == "is_full"
    finally:
        service.initialization.reset()
        if uploaded_folder is not None:
            shutil.rmtree(uploaded_folder, ignore_errors=True)


def test_initialization_api_selects_3d_choice(monkeypatch):
    _patch_recover_3d_all(monkeypatch)
    service.initialization.reset()
    uploaded_folder: Path | None = None
    try:
        payload = upload_initialization_folder(
            InitializationUploadFolderRequest(files=_uploaded_png_files(), image_choice="first")
        )
        uploaded_folder = Path(payload["image_folder"])
        session_id = payload["session_id"]
        service.initialization.set_is_full(session_id, False)
        _submit_adjacent_sides(service.initialization, session_id)
        _submit(service.initialization, session_id, "w", 30, 30)
        _submit_kernel(service.initialization, session_id)
        service.initialization.choose_corner(session_id, "A")

        payload = choose_initialization_3d_choice(
            Initialization3DChoiceRequest(session_id=session_id, choice=1)
        )

        assert payload["status"] == "ready_for_3d"
        assert payload["selected_3d_choice"] == 1
    finally:
        service.initialization.reset()
        if uploaded_folder is not None:
            shutil.rmtree(uploaded_folder, ignore_errors=True)
