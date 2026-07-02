import numpy as np
import pytest

import crystallization_mpc.apps.gsensor.detection.initialize_DSCGR as module
from crystallization_mpc.apps.gsensor.initialization import (
    GsensorInitializationManager,
    InitializationSession,
)


def _manager_with_ready_session(tmp_path):
    manager = GsensorInitializationManager()
    image_path = tmp_path / "imgs" / "frame.png"
    session = InitializationSession(
        session_id="session-1",
        image_folder=str(image_path.parent),
        images=[str(image_path)],
        selected_image=str(image_path),
        is_full=True,
        corner="A",
        selected_3d_choice=1,
        recovered_3d={
            "M": [0.0, 0.0, 0.0],
            "W": [1.0, 0.0, 0.0],
            "U": [0.0, 1.0, 0.0],
            "V": [0.0, 0.0, 1.0],
            "show_3d": {"type": "patch"},
        },
        status="ready_for_3d",
    )
    session.points.update(
        {
            "u_ad.t": [0.0, 0.0, 0.0],
            "u_ad.e": [10.0, 0.0, 0.0],
            "u_ad.o": [0.0, 1.0, 0.0],
            "v_ad.t": [0.0, 0.0, 0.0],
            "v_ad.e": [0.0, 10.0, 0.0],
            "v_ad.o": [1.0, 0.0, 0.0],
            "u_op.t": [10.0, 0.0, 0.0],
            "u_op.e": [10.0, 10.0, 0.0],
            "u_op.o": [9.0, 0.0, 0.0],
            "v_op.t": [0.0, 10.0, 0.0],
            "v_op.e": [10.0, 10.0, 0.0],
            "v_op.o": [0.0, 9.0, 0.0],
        }
    )
    for index in range(1, 5):
        session.points[f"kernel.k_c_cell.{index}"] = [float(index), 0.0, 0.0]
        session.points[f"kernel.k_o_cell.{index}"] = [float(index), 1.0, 0.0]
    manager.sessions[session.session_id] = session
    manager.active_session_id = session.session_id
    return manager, session


def test_initialize_DSCGR_finalizes_completed_web_session(monkeypatch, tmp_path):
    manager, session = _manager_with_ready_session(tmp_path)

    def fake_calc_2d_3d_info(M, W, U, V, u_op, v_op, u_ad, v_ad, is_full):
        assert M == session.recovered_3d["M"]
        assert W == session.recovered_3d["W"]
        assert U == session.recovered_3d["U"]
        assert V == session.recovered_3d["V"]
        assert u_op is not None
        assert v_op is not None
        assert u_ad is not None
        assert v_ad is not None
        assert is_full is True
        return "u_struct", "v_struct"

    monkeypatch.setattr(module, "calc_2d_3d_info", fake_calc_2d_3d_info)

    uv_struct_list, kernel = module.initialize_DSCGR(
        manager,
        session.session_id,
    )

    assert uv_struct_list == ["u_struct", "v_struct"]
    np.testing.assert_allclose(kernel.k_c_cell[0], [1.0, 0.0, 0.0])
    np.testing.assert_allclose(kernel.k_o_cell[3], [4.0, 1.0, 0.0])


def test_initialize_DSCGR_requires_selected_3d_candidate(tmp_path):
    manager, session = _manager_with_ready_session(tmp_path)
    session.status = "ready_for_3d_choice"
    session.recovered_3d = None

    with pytest.raises(ValueError, match="select a 3D candidate"):
        module.initialize_DSCGR(manager, session.session_id)


def test_initialize_DSCGR_requires_complete_kernel_points(tmp_path):
    manager, session = _manager_with_ready_session(tmp_path)
    session.points.pop("kernel.k_o_cell.4")

    with pytest.raises(ValueError, match="kernel points are incomplete"):
        module.initialize_DSCGR(manager, session.session_id)


def test_initialize_DSCGR_does_not_write_mat_file(monkeypatch, tmp_path):
    manager, session = _manager_with_ready_session(tmp_path)
    monkeypatch.setattr(module, "calc_2d_3d_info", lambda *args: ("u_struct", "v_struct"))

    uv_struct_list, kernel = module.initialize_DSCGR(
        manager,
        session.session_id,
    )

    assert uv_struct_list == ["u_struct", "v_struct"]
    np.testing.assert_allclose(kernel.k_c_cell[0], [1.0, 0.0, 0.0])
    assert not list(tmp_path.rglob("*.mat"))
