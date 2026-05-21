from pathlib import Path
import shutil
from uuid import uuid4

import pytest
from fastapi import HTTPException

from crystallization_mpc.apps.gsensor.app import (
    InitializationFolderRequest,
    InitializationResetRequest,
    get_initialization_step,
    reset_initialization,
    service,
    start_initialization,
)
from crystallization_mpc.apps.gsensor.initialization import GsensorInitializationManager


def _write_png_header(path: Path, width: int = 64, height: int = 48) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


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


def test_full_initialization_steps_compute_u_v_w():
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

        assert payload["status"] == "ready_for_3d"
        assert payload["corner"] == "B"
        assert payload["current_step"] is None
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


def test_initialization_api_folder_reset_and_invalid_session():
    folder = _make_image_folder()
    service.initialization.reset()
    try:
        payload = start_initialization(
            InitializationFolderRequest(folder=str(folder), image_choice="first")
        )
        session_id = payload["session_id"]

        with pytest.raises(HTTPException) as exc:
            get_initialization_step(session_id="missing")
        assert exc.value.status_code == 400

        payload = reset_initialization(InitializationResetRequest(session_id=session_id))
        assert payload["status"] == "not_started"
    finally:
        _cleanup_image_folder(folder)
