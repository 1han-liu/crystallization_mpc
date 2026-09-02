"""Sparse Lucas-Kanade optical-flow alignment from the reference pipeline."""

from __future__ import annotations

import cv2
import numpy as np

KERNEL_OPEN_SIZE = (100, 100)
KERNEL_CLOSE_SIZE = (100, 100)
KERNEL_ERODE_SIZE = (200, 200)
LK_PARAMS = {
    "winSize": (21, 21),
    "maxLevel": 3,
    "criteria": (
        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
        30,
        0.01,
    ),
}
FEATURE_PARAMS = {
    "maxCorners": 200,
    "qualityLevel": 0.01,
    "minDistance": 7,
    "blockSize": 7,
}


def force_euclidean(matrix_2x3: np.ndarray) -> np.ndarray:
    """Project an affine estimate to rotation plus translation."""

    linear = matrix_2x3[:, :2].astype(np.float64)
    left, _, right = np.linalg.svd(linear)
    rotation = left @ right
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1
        rotation = left @ right
    result = np.eye(2, 3, dtype=np.float32)
    result[:, :2] = rotation.astype(np.float32)
    result[:, 2] = matrix_2x3[:, 2]
    return result


def estimate_euclidean_from_tracks(prev_pts, curr_pts) -> np.ndarray:
    """Estimate the previous-to-current transform from valid LK tracks."""

    warp = np.eye(2, 3, dtype=np.float32)
    if prev_pts is None or curr_pts is None or len(prev_pts) == 0:
        return warp
    previous = np.asarray(prev_pts, dtype=np.float32).reshape(-1, 2)
    current = np.asarray(curr_pts, dtype=np.float32).reshape(-1, 2)
    if len(previous) >= 3:
        estimate, _ = cv2.estimateAffinePartial2D(
            previous,
            current,
            method=cv2.RANSAC,
        )
        if estimate is not None:
            return force_euclidean(estimate)
    warp[:, 2] = np.median(current - previous, axis=0)
    return warp


def get_stable_mask(mask_raw: np.ndarray | None) -> np.ndarray | None:
    if mask_raw is None:
        return None
    kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_OPEN_SIZE)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_CLOSE_SIZE)
    kernel_erode = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_ERODE_SIZE)
    opened = cv2.morphologyEx(mask_raw, cv2.MORPH_OPEN, kernel_open)
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel_close)
    return cv2.morphologyEx(closed, cv2.MORPH_ERODE, kernel_erode)


def enhance_contrast(img_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8)).apply(gray)


def align_one_frame_optical_flow(
    prev_gray_enhanced: np.ndarray,
    curr_gray: np.ndarray,
    prev_mask: np.ndarray,
    curr_mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float, float, int, np.ndarray]:
    """Align one current frame to the supplied aligned reference frame."""

    height, width = curr_gray.shape[:2]
    current_bgr = cv2.cvtColor(curr_gray, cv2.COLOR_GRAY2BGR)
    current_enhanced = enhance_contrast(current_bgr)
    dx = dy = 0.0
    point_count = 0
    warp_matrix = np.eye(2, 3, dtype=np.float32)
    previous_core = get_stable_mask(prev_mask)
    if previous_core is not None and cv2.countNonZero(previous_core) > 100:
        previous_points = cv2.goodFeaturesToTrack(
            prev_gray_enhanced,
            mask=previous_core,
            **FEATURE_PARAMS,
        )
        if previous_points is not None:
            current_points, status, _ = cv2.calcOpticalFlowPyrLK(
                prev_gray_enhanced,
                current_enhanced,
                previous_points,
                None,
                **LK_PARAMS,
            )
            if current_points is not None and status is not None:
                good_previous = previous_points[status == 1]
                good_current = current_points[status == 1]
                if len(good_current) > 0:
                    warp_matrix = estimate_euclidean_from_tracks(
                        good_previous,
                        good_current,
                    )
                    inverse = cv2.invertAffineTransform(warp_matrix)
                    dx = float(inverse[0, 2])
                    dy = float(inverse[1, 2])
                    point_count = len(good_current)

    aligned_mask = cv2.warpAffine(
        curr_mask,
        warp_matrix,
        (width, height),
        flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return aligned_mask, current_enhanced, dx, dy, point_count, warp_matrix


__all__ = [
    "align_one_frame_optical_flow",
    "enhance_contrast",
    "estimate_euclidean_from_tracks",
    "force_euclidean",
    "get_stable_mask",
]
