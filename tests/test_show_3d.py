import numpy as np

from crystallization_mpc.apps.gsensor.utils.show_3d import show_3d


def test_show_3d_returns_matlab_patch_payload_with_flipped_z():
    payload = show_3d(
        None,
        [1.0, 2.0, 3.0],
        [4.0, 5.0, -6.0],
        [7.0, 8.0, 0.0],
        [9.0, 10.0, 11.0],
    )

    assert payload["type"] == "patch"
    np.testing.assert_allclose(
        payload["vertices"],
        [
            [1.0, 2.0, -3.0],
            [4.0, 5.0, 6.0],
            [7.0, 8.0, -0.0],
            [9.0, 10.0, -11.0],
        ]
    )
    assert payload["faces"] == [[1, 2, 3], [1, 2, 4], [1, 3, 4], [2, 3, 4]]
    assert payload["face_vertex_cdata"] == [1.0, 0.0, 0.0, 0.0]
    assert payload["face_color"] == "flat"
    assert payload["face_alpha"] == 0.1
    assert "reference_2d" not in payload


def test_show_3d_pads_2d_points_to_3d():
    payload = show_3d(None, [1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0])

    np.testing.assert_allclose(
        payload["vertices"],
        [
            [1.0, 2.0, -0.0],
            [3.0, 4.0, -0.0],
            [5.0, 6.0, -0.0],
            [7.0, 8.0, -0.0],
        ]
    )
