from __future__ import annotations

from unittest import mock

import cv2
import numpy as np
import pytest

from crystallization_mpc.apps.gsensor.alignment import ecc, sift


def _rigid_points() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    current = np.array(
        [[20.0, 30.0], [80.0, 25.0], [90.0, 95.0], [25.0, 85.0]],
        dtype=np.float64,
    )
    angle = np.deg2rad(7.0)
    rotation = np.array(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]]
    )
    translation = np.array([12.5, -8.0])
    reference = (rotation @ current.T).T + translation
    return reference, current, rotation, translation


def test_sift_mask_preparation_matches_reference_operations() -> None:
    processed = np.zeros((256, 256), dtype=np.uint8)
    raw = np.zeros_like(processed)
    processed[20:236, 20:236] = 255
    processed[100:130, 100:130] = 0
    raw[10:246, 10:246] = 255
    seed = sift.build_sift_seed_mask(processed, raw)
    modified, soft, inner = sift.prepare_matching_masks(seed)
    expected = cv2.erode(
        cv2.morphologyEx(
            cv2.bitwise_and(processed, raw),
            cv2.MORPH_CLOSE,
            np.ones((75, 75), dtype=np.uint8),
            borderType=cv2.BORDER_REPLICATE,
        ),
        np.ones((25, 25), dtype=np.uint8),
        borderType=cv2.BORDER_REPLICATE,
    )
    assert np.array_equal(modified, expected)
    assert soft.dtype == np.float32
    assert np.array_equal(inner, soft > 0.95)


def test_sift_kabsch_keeps_its_current_to_reference_convention() -> None:
    reference, current, rotation, translation = _rigid_points()
    matrix = sift.estimate_rigid_transform(reference, current)
    assert np.allclose(matrix[:, :2], rotation, atol=1e-5)
    assert np.allclose(matrix[:, 2], translation, atol=1e-5)
    assert np.allclose(matrix[:, :2].T @ matrix[:, :2], np.eye(2), atol=1e-5)
    assert np.linalg.det(matrix[:, :2]) == pytest.approx(1.0, abs=1e-5)


def test_sift_magsac_failure_is_explicit() -> None:
    points = np.arange(16, dtype=np.float32).reshape(8, 2)
    with mock.patch.object(cv2, "findFundamentalMat", return_value=(None, None)):
        with pytest.raises(sift.AlignmentFailure):
            sift.calculate_magsac_inliers(points, points)


def test_loftr_masks_and_geometry_when_optional_dependencies_are_installed() -> None:
    pytest.importorskip("torch")
    pytest.importorskip("kornia.feature")
    from crystallization_mpc.apps.gsensor.alignment import loftr

    processed = np.zeros((256, 256), dtype=np.uint8)
    raw = np.zeros_like(processed)
    processed[20:236, 20:236] = 255
    raw[10:246, 10:246] = 255
    seed = loftr.build_loftr_seed_mask(processed, raw)
    modified, soft, inner = loftr.prepare_matching_masks(seed)
    assert modified.shape == seed.shape
    assert soft.dtype == np.float32
    assert np.array_equal(inner, soft > 0.95)

    reference, current, rotation, translation = _rigid_points()
    matrix = loftr.estimate_rigid_transform(reference, current)
    assert np.allclose(matrix[:, :2], rotation, atol=1e-5)
    assert np.allclose(matrix[:, 2], translation, atol=1e-5)


def test_ecc_reset_keeps_applied_transform_and_resets_only_next_warm_start() -> None:
    gray = np.zeros((256, 256), dtype=np.uint8)
    gray[60:200, 70:210] = 180
    mask = np.zeros_like(gray)
    mask[40:220, 40:220] = 255
    estimated = np.array([[1.0, 0.0, 4.0], [0.0, 1.0, -3.0]], dtype=np.float32)
    with mock.patch.object(ecc, "find_transform_ecc_pyramid", return_value=estimated):
        _aligned, _enhanced, applied, buffer, diagnostics = ecc.align_one_frame_ecc(
            ecc.enhance_for_ecc(gray),
            gray,
            mask,
            frame_idx=ecc.RESET_INTERVAL,
        )
    assert diagnostics["success"]
    assert np.array_equal(applied, estimated)
    assert np.array_equal(diagnostics["next_warp"], np.eye(2, 3, dtype=np.float32))
    assert buffer == []
