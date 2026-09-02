"""StableGSensor-style SIFT alignment for the DSCGR pipeline.

This module intentionally owns its complete matching pipeline.  It does not
reuse implementation details from ``3_loftr.py``: the two alignment methods
remain independent choices in ``dscgr.py``.

The mask produced by ``2_postprocess.py`` remains authoritative for Hough and
growth-rate measurements.  Masks prepared here are temporary SIFT matching
masks used to suppress unstable keypoints near the crystal boundary.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Optional, Sequence, Tuple

import cv2
import numpy as np


RATIO_THRESHOLD = 0.75

CLOSE_KERNEL_SIZE = (75, 75)
ERODE_KERNEL_SIZE = (25, 25)
SOFT_FADE_DISTANCE = 50
INNER_THRESHOLD = 0.95
SOFT_BACKGROUND_VALUE = 1.0

MIN_MASKED_MATCHES = 8
MIN_RIGID_INLIERS = 2
MAGSAC_REPROJ_THRESHOLD = 0.5
MAGSAC_CONFIDENCE = 0.999
MAGSAC_MAX_ITERS = 100000

os.environ["OPENCV_LOG_LEVEL"] = "SILENT"


class AlignmentFailure(RuntimeError):
    """Expected per-frame alignment failure that should trigger fallback."""


def _binary_mask(mask: np.ndarray, name: str) -> np.ndarray:
    if mask is None:
        raise ValueError(f"{name} must not be None")
    array = np.asarray(mask)
    if array.ndim != 2:
        raise ValueError(f"{name} must be a 2-D mask, got shape {array.shape}")
    return np.where(array > 0, 255, 0).astype(np.uint8)


def _gray_u8(image: np.ndarray, name: str) -> np.ndarray:
    if image is None:
        raise ValueError(f"{name} must not be None")
    array = np.asarray(image)
    if array.ndim == 3:
        array = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
    if array.ndim != 2:
        raise ValueError(f"{name} must be grayscale or BGR, got shape {array.shape}")
    if array.dtype == np.uint8:
        return array.copy()
    array = array.astype(np.float32)
    if array.size and float(np.nanmax(array)) <= 1.0:
        array = array * 255.0
    return np.clip(array, 0.0, 255.0).astype(np.uint8)


def build_sift_seed_mask(
    processed_measurement_mask: np.ndarray,
    current_raw_mask: np.ndarray,
) -> np.ndarray:
    """Restrict the temporal postprocess result to current-frame YOLO support."""
    processed = _binary_mask(processed_measurement_mask, "processed_measurement_mask")
    raw = _binary_mask(current_raw_mask, "current_raw_mask")
    if processed.shape != raw.shape:
        raise ValueError(
            "processed_measurement_mask and current_raw_mask must have the same "
            f"shape, got {processed.shape} and {raw.shape}"
        )
    return cv2.bitwise_and(processed, raw)


def soften_mask(mask: np.ndarray, fade_distance: int = SOFT_FADE_DISTANCE) -> np.ndarray:
    """Create StableGSensor's inward distance-based mask in the range [0, 1]."""
    if fade_distance <= 0:
        raise ValueError("fade_distance must be positive")
    binary = _binary_mask(mask, "mask")
    pad = int(fade_distance)
    padded = cv2.copyMakeBorder(
        binary, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0
    )
    distance = cv2.distanceTransform(padded, cv2.DIST_L2, 5)
    distance = distance[pad:-pad, pad:-pad]
    return np.clip(distance / float(fade_distance), 0.0, 1.0).astype(np.float32)


def prepare_matching_masks(
    seed_mask: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return closed/eroded, soft, and strict-inner SIFT masks."""
    binary = _binary_mask(seed_mask, "seed_mask")
    close_kernel = np.ones(CLOSE_KERNEL_SIZE, dtype=np.uint8)
    erode_kernel = np.ones(ERODE_KERNEL_SIZE, dtype=np.uint8)
    modified = cv2.erode(
        cv2.morphologyEx(
            binary,
            cv2.MORPH_CLOSE,
            close_kernel,
            borderType=cv2.BORDER_REPLICATE,
        ),
        erode_kernel,
        iterations=1,
        borderType=cv2.BORDER_REPLICATE,
    )
    soft = soften_mask(modified)
    inner = soft > INNER_THRESHOLD
    return modified, soft, inner


def apply_soft_mask(
    image_gray: np.ndarray,
    soft_mask: np.ndarray,
    background_value: float = SOFT_BACKGROUND_VALUE,
) -> np.ndarray:
    """Blend a grayscale image with a white background using a soft mask."""
    image = _gray_u8(image_gray, "image_gray").astype(np.float32) / 255.0
    soft = np.asarray(soft_mask, dtype=np.float32)
    if image.shape != soft.shape:
        raise ValueError(
            f"image_gray and soft_mask must have the same shape, got "
            f"{image.shape} and {soft.shape}"
        )
    return image * soft + (1.0 - soft) * float(background_value)


def prepare_image_for_sift(image_gray: np.ndarray, soft_mask: np.ndarray) -> np.ndarray:
    """Return the uint8 soft-masked image consumed by OpenCV SIFT."""
    masked = apply_soft_mask(image_gray, soft_mask)
    return np.clip(np.rint(masked * 255.0), 0.0, 255.0).astype(np.uint8)


def init_sift():
    """Create the StableGSensor SIFT detector and L2 brute-force matcher."""
    if not hasattr(cv2, "SIFT_create"):
        raise RuntimeError("This OpenCV build does not provide cv2.SIFT_create")
    return cv2.SIFT_create(), cv2.BFMatcher(cv2.NORM_L2)


def select_ratio_matches(
    matches_knn: Sequence[Sequence[cv2.DMatch]],
    ratio_threshold: float = RATIO_THRESHOLD,
) -> list:
    """Apply StableGSensor's two-neighbour Lowe ratio test."""
    if ratio_threshold <= 0:
        raise ValueError("ratio_threshold must be positive")
    selected = []
    for pair in matches_knn:
        if len(pair) != 2:
            continue
        first, second = pair
        if first.distance < ratio_threshold * second.distance:
            selected.append(first)
    return selected


def filter_matches_by_dual_masks(
    reference_points: np.ndarray,
    current_points: np.ndarray,
    reference_inner_mask: np.ndarray,
    current_inner_mask: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """Keep correspondences whose rounded endpoints lie inside both masks."""
    reference = np.asarray(reference_points, dtype=np.float32).reshape(-1, 2)
    current = np.asarray(current_points, dtype=np.float32).reshape(-1, 2)
    if len(reference) != len(current):
        raise ValueError("reference_points and current_points must have equal length")

    reference = np.round(reference).astype(np.int32)
    current = np.round(current).astype(np.int32)
    ref_mask = np.asarray(reference_inner_mask, dtype=bool)
    cur_mask = np.asarray(current_inner_mask, dtype=bool)
    if ref_mask.ndim != 2 or cur_mask.ndim != 2:
        raise ValueError("inner masks must be two-dimensional")

    keep = np.zeros(len(reference), dtype=bool)
    for index, ((ref_x, ref_y), (cur_x, cur_y)) in enumerate(
        zip(reference, current)
    ):
        ref_valid = 0 <= ref_x < ref_mask.shape[1] and 0 <= ref_y < ref_mask.shape[0]
        cur_valid = 0 <= cur_x < cur_mask.shape[1] and 0 <= cur_y < cur_mask.shape[0]
        if ref_valid and cur_valid and ref_mask[ref_y, ref_x] and cur_mask[cur_y, cur_x]:
            keep[index] = True
    return reference[keep].astype(np.float32), current[keep].astype(np.float32)


def calculate_magsac_inliers(
    reference_points: np.ndarray,
    current_points: np.ndarray,
) -> np.ndarray:
    """Return a boolean Fundamental-Matrix USAC_MAGSAC inlier mask."""
    reference = np.asarray(reference_points, dtype=np.float32).reshape(-1, 2)
    current = np.asarray(current_points, dtype=np.float32).reshape(-1, 2)
    if len(reference) != len(current):
        raise AlignmentFailure("reference and current match arrays have unequal length")
    if len(reference) < MIN_MASKED_MATCHES:
        raise AlignmentFailure(
            f"dual-mask matches {len(reference)} < {MIN_MASKED_MATCHES}"
        )
    _, inliers = cv2.findFundamentalMat(
        reference,
        current,
        cv2.USAC_MAGSAC,
        MAGSAC_REPROJ_THRESHOLD,
        MAGSAC_CONFIDENCE,
        MAGSAC_MAX_ITERS,
    )
    if inliers is None:
        raise AlignmentFailure("USAC_MAGSAC did not return an inlier mask")
    mask = np.asarray(inliers).reshape(-1).astype(bool)
    if len(mask) != len(reference):
        raise AlignmentFailure("USAC_MAGSAC returned an invalid inlier-mask length")
    count = int(np.count_nonzero(mask))
    if count < MIN_RIGID_INLIERS:
        raise AlignmentFailure(f"MAGSAC inliers {count} < {MIN_RIGID_INLIERS}")
    return mask


def estimate_rigid_transform(
    reference_points: np.ndarray,
    current_points: np.ndarray,
) -> np.ndarray:
    """Estimate current-to-reference rotation and translation using Kabsch."""
    target = np.asarray(reference_points, dtype=np.float64).reshape(-1, 2)
    source = np.asarray(current_points, dtype=np.float64).reshape(-1, 2)
    if len(target) != len(source) or len(source) < MIN_RIGID_INLIERS:
        raise AlignmentFailure("not enough paired inliers for rigid transformation")
    if not np.all(np.isfinite(target)) or not np.all(np.isfinite(source)):
        raise AlignmentFailure("rigid-transform input contains NaN or Inf")

    source_center = np.mean(source, axis=0)
    target_center = np.mean(target, axis=0)
    source_centered = source - source_center
    target_centered = target - target_center
    if np.linalg.norm(source_centered) <= 1e-12 or np.linalg.norm(target_centered) <= 1e-12:
        raise AlignmentFailure("rigid-transform correspondences are degenerate")

    u, _, vt = np.linalg.svd(source_centered.T @ target_centered)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1, :] *= -1
        rotation = vt.T @ u.T
    translation = target_center - rotation @ source_center

    matrix = np.column_stack((rotation, translation)).astype(np.float32)
    if matrix.shape != (2, 3) or not np.all(np.isfinite(matrix)):
        raise AlignmentFailure("rigid transformation is invalid")
    if not np.allclose(rotation.T @ rotation, np.eye(2), atol=1e-5):
        raise AlignmentFailure("estimated rotation is not orthogonal")
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-5):
        raise AlignmentFailure("estimated rotation determinant is not +1")
    return matrix


def _validated_fallback_matrix(matrix: Optional[np.ndarray]) -> np.ndarray:
    if matrix is None:
        return np.eye(2, 3, dtype=np.float32)
    candidate = np.asarray(matrix, dtype=np.float32)
    if candidate.shape != (2, 3) or not np.all(np.isfinite(candidate)):
        return np.eye(2, 3, dtype=np.float32)
    rotation = candidate[:, :2].astype(np.float64)
    if not np.allclose(rotation.T @ rotation, np.eye(2), atol=1e-4):
        return np.eye(2, 3, dtype=np.float32)
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-4):
        return np.eye(2, 3, dtype=np.float32)
    return candidate.copy()


def _masked_residual(
    reference_gray: np.ndarray,
    current_gray: np.ndarray,
    reference_mask: np.ndarray,
    current_mask: np.ndarray,
) -> float:
    if not (
        np.asarray(reference_gray).shape[:2]
        == np.asarray(current_gray).shape[:2]
        == np.asarray(reference_mask).shape
        == np.asarray(current_mask).shape
    ):
        return float("nan")
    valid = (np.asarray(reference_mask) > 0) & (np.asarray(current_mask) > 0)
    reference = _gray_u8(reference_gray, "reference_gray").astype(np.float32)
    current = _gray_u8(current_gray, "current_gray").astype(np.float32)
    if not np.any(valid):
        return float(np.mean(np.abs(reference - current)))
    return float(np.mean(np.abs(reference[valid] - current[valid])))


def initial_alignment_diagnostics() -> Dict[str, object]:
    """Diagnostics for the unmodified first frame of a sequence."""
    return {
        "reference_keypoints": 0,
        "current_keypoints": 0,
        "ratio_matches": 0,
        "dual_mask_matches": 0,
        "magsac_inliers": 0,
        "inlier_ratio": 0.0,
        "tx": 0.0,
        "ty": 0.0,
        "rotation_deg": 0.0,
        "residual_before": 0.0,
        "residual_after": 0.0,
        "runtime_s": 0.0,
        "success": True,
        "fallback_reason": "initial_frame",
    }


def align_one_frame_sift(
    previous_aligned_gray: np.ndarray,
    current_gray: np.ndarray,
    previous_aligned_seed_mask: np.ndarray,
    current_seed_mask: np.ndarray,
    current_measurement_mask: np.ndarray,
    sift_detector,
    bf_matcher,
    fallback_matrix: Optional[np.ndarray] = None,
):
    """Align one frame using independent StableGSensor-style SIFT matching."""
    start = time.perf_counter()
    diagnostics = initial_alignment_diagnostics()
    diagnostics["success"] = False
    diagnostics["fallback_reason"] = ""
    selected_matrix = _validated_fallback_matrix(fallback_matrix)

    previous = _gray_u8(previous_aligned_gray, "previous_aligned_gray")
    current = _gray_u8(current_gray, "current_gray")
    previous_seed = _binary_mask(previous_aligned_seed_mask, "previous_aligned_seed_mask")
    current_seed = _binary_mask(current_seed_mask, "current_seed_mask")
    measurement = _binary_mask(current_measurement_mask, "current_measurement_mask")
    if not (current.shape == current_seed.shape == measurement.shape):
        raise ValueError("current SIFT image and masks must all have the same shape")

    try:
        if not (previous.shape == current.shape == previous_seed.shape):
            raise AlignmentFailure("reference and current SIFT shapes do not match")

        _, previous_soft, previous_inner = prepare_matching_masks(previous_seed)
        _, current_soft, current_inner = prepare_matching_masks(current_seed)
        if not np.any(previous_inner):
            raise AlignmentFailure("reference inner mask is empty")
        if not np.any(current_inner):
            raise AlignmentFailure("current inner mask is empty")

        previous_masked = prepare_image_for_sift(previous, previous_soft)
        current_masked = prepare_image_for_sift(current, current_soft)
        reference_keypoints, reference_descriptors = sift_detector.detectAndCompute(
            previous_masked, None
        )
        current_keypoints, current_descriptors = sift_detector.detectAndCompute(
            current_masked, None
        )
        diagnostics["reference_keypoints"] = int(len(reference_keypoints or []))
        diagnostics["current_keypoints"] = int(len(current_keypoints or []))
        if reference_descriptors is None:
            raise AlignmentFailure("reference SIFT descriptors are missing")
        if current_descriptors is None:
            raise AlignmentFailure("current SIFT descriptors are missing")

        matches_knn = bf_matcher.knnMatch(
            reference_descriptors, current_descriptors, k=2
        )
        ratio_matches = select_ratio_matches(matches_knn)
        diagnostics["ratio_matches"] = int(len(ratio_matches))

        reference_points = np.asarray(
            [reference_keypoints[match.queryIdx].pt for match in ratio_matches],
            dtype=np.float32,
        ).reshape(-1, 2)
        current_points = np.asarray(
            [current_keypoints[match.trainIdx].pt for match in ratio_matches],
            dtype=np.float32,
        ).reshape(-1, 2)
        reference_points, current_points = filter_matches_by_dual_masks(
            reference_points,
            current_points,
            previous_inner,
            current_inner,
        )
        diagnostics["dual_mask_matches"] = int(len(reference_points))

        inliers = calculate_magsac_inliers(reference_points, current_points)
        diagnostics["magsac_inliers"] = int(np.count_nonzero(inliers))
        diagnostics["inlier_ratio"] = float(
            diagnostics["magsac_inliers"] / len(reference_points)
        )
        selected_matrix = estimate_rigid_transform(
            reference_points[inliers], current_points[inliers]
        )
        diagnostics["success"] = True
    except Exception as error:
        diagnostics["fallback_reason"] = f"{type(error).__name__}: {error}"

    height, width = current.shape
    warped_image = cv2.warpAffine(
        current,
        selected_matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    aligned_measurement_mask = cv2.warpAffine(
        measurement,
        selected_matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    aligned_seed_mask = cv2.warpAffine(
        current_seed,
        selected_matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )

    diagnostics["tx"] = float(selected_matrix[0, 2])
    diagnostics["ty"] = float(selected_matrix[1, 2])
    diagnostics["rotation_deg"] = float(
        np.degrees(np.arctan2(selected_matrix[1, 0], selected_matrix[0, 0]))
    )
    diagnostics["residual_before"] = _masked_residual(
        previous, current, previous_seed, current_seed
    )
    diagnostics["residual_after"] = _masked_residual(
        previous, warped_image, previous_seed, aligned_seed_mask
    )
    diagnostics["runtime_s"] = float(time.perf_counter() - start)

    return (
        aligned_measurement_mask,
        aligned_seed_mask,
        warped_image,
        selected_matrix,
        diagnostics,
    )
