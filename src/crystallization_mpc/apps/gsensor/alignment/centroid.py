"""Stable-centroid translation alignment extracted from the reference pipeline."""

from __future__ import annotations

import cv2
import numpy as np

KERNEL_OPEN_SIZE = (15, 15)
KERNEL_CLOSE_SIZE = (25, 25)
KERNEL_ERODE_SIZE = (15, 15)


def get_stable_mask(mask_raw: np.ndarray | None) -> np.ndarray | None:
    if mask_raw is None:
        return None
    kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_OPEN_SIZE)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_CLOSE_SIZE)
    kernel_erode = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_ERODE_SIZE)
    opened = cv2.morphologyEx(mask_raw, cv2.MORPH_OPEN, kernel_open)
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel_close)
    return cv2.morphologyEx(closed, cv2.MORPH_ERODE, kernel_erode)


def get_centroid(mask: np.ndarray | None) -> tuple[float, float] | None:
    if mask is None:
        return None
    moments = cv2.moments(mask)
    if moments["m00"] == 0:
        return None
    return moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]


def align_one_frame_centroid(
    curr_mask: np.ndarray,
    prev_centroid_stable: tuple[float, float] | None,
    cumulative_dx: float,
    cumulative_dy: float,
) -> tuple[np.ndarray, tuple[float, float] | None, float, float]:
    """Align one mask using cumulative stable-centroid displacement."""

    height, width = curr_mask.shape[:2]
    curr_centroid = get_centroid(get_stable_mask(curr_mask))
    if curr_centroid is not None and prev_centroid_stable is not None:
        cumulative_dx += prev_centroid_stable[0] - curr_centroid[0]
        cumulative_dy += prev_centroid_stable[1] - curr_centroid[1]

    matrix = np.float32(
        [[1, 0, round(cumulative_dx)], [0, 1, round(cumulative_dy)]]
    )
    aligned_mask = cv2.warpAffine(
        curr_mask,
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderValue=0,
    )
    next_centroid = curr_centroid if curr_centroid is not None else prev_centroid_stable
    return aligned_mask, next_centroid, cumulative_dx, cumulative_dy


__all__ = ["align_one_frame_centroid", "get_centroid", "get_stable_mask"]
