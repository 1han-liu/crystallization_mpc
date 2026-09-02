"""FFT cross-correlation mask alignment from the reference pipeline."""

from __future__ import annotations

import cv2
import numpy as np

MAX_RADIUS = 50
LOCAL_REFINE_RADIUS = 15


def find_best_shift_fft(
    prev_mask: np.ndarray,
    curr_mask: np.ndarray,
    max_radius: int = MAX_RADIUS,
) -> tuple[float, float, bool, int]:
    height, width = prev_mask.shape
    previous = (prev_mask > 127).astype(np.float32)
    current = (curr_mask > 127).astype(np.float32)
    if np.array_equal(previous, current):
        return 0.0, 0.0, True, 0

    previous_fft = np.fft.rfft2(previous, s=(height, width))
    current_fft = np.fft.rfft2(current, s=(height, width))
    correlation = np.fft.fftshift(
        np.fft.irfft2(previous_fft * np.conj(current_fft), s=(height, width))
    )
    center_y, center_x = height // 2, width // 2
    y0 = max(0, center_y - max_radius)
    y1 = min(height, center_y + max_radius + 1)
    x0 = max(0, center_x - max_radius)
    x1 = min(width, center_x + max_radius + 1)
    roi = correlation[y0:y1, x0:x1]
    maximum = roi.max()
    ys, xs = np.where(roi >= maximum - 0.5)
    absolute_y = y0 + ys
    absolute_x = x0 + xs
    best = np.argmin((absolute_y - center_y) ** 2 + (absolute_x - center_x) ** 2)
    dy = float(absolute_y[best] - center_y)
    dx = float(absolute_x[best] - center_x)
    target_pixels = int(previous.sum())
    overlap = int(round(float(maximum)))
    return dx, dy, overlap >= target_pixels, max(0, target_pixels - overlap)


def _local_refine(
    prev_mask: np.ndarray,
    curr_mask: np.ndarray,
    dx_initial: float,
    dy_initial: float,
    radius: int = LOCAL_REFINE_RADIUS,
) -> tuple[float, float, int]:
    height, width = curr_mask.shape
    previous = prev_mask > 127
    best_overlap = -1
    best_dx, best_dy = dx_initial, dy_initial
    for delta_y in range(-radius, radius + 1):
        for delta_x in range(-radius, radius + 1):
            tx = round(dx_initial) + delta_x
            ty = round(dy_initial) + delta_y
            shifted = cv2.warpAffine(
                curr_mask,
                np.float32([[1, 0, tx], [0, 1, ty]]),
                (width, height),
                flags=cv2.INTER_NEAREST,
                borderValue=0,
            )
            overlap = int(np.count_nonzero(previous & (shifted > 127)))
            if overlap > best_overlap:
                best_overlap = overlap
                best_dx, best_dy = float(tx), float(ty)
    left_pixels = int(np.count_nonzero(previous)) - best_overlap
    return best_dx, best_dy, max(0, left_pixels)


def align_one_frame(
    prev_mask: np.ndarray,
    curr_mask: np.ndarray,
) -> tuple[np.ndarray, float, float, int]:
    height, width = curr_mask.shape
    dx, dy, perfect, left_pixels = find_best_shift_fft(prev_mask, curr_mask)
    if LOCAL_REFINE_RADIUS > 0 and not perfect:
        dx, dy, left_pixels = _local_refine(prev_mask, curr_mask, dx, dy)
    if dx == 0.0 and dy == 0.0:
        return curr_mask.copy(), 0.0, 0.0, left_pixels
    matrix = np.float32([[1, 0, round(dx)], [0, 1, round(dy)]])
    aligned = cv2.warpAffine(
        curr_mask,
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderValue=0,
    )
    return aligned, dx, dy, left_pixels


__all__ = ["align_one_frame", "find_best_shift_fft"]
