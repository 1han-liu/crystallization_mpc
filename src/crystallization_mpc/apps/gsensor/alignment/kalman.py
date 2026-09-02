"""
3_kalman.py — Kalman-filter-based centroid alignment.

Uses a 4-state Kalman filter (x, y, vx, vy) to smooth the crystal
centroid trajectory, then aligns each frame so the smoothed centroid
stays fixed.  Works per-frame (uses only the current measurement +
previous KF state), so it is suitable for the real-time loop in dscgr.py.

Importable functions
--------------------
get_stable_mask(mask_raw) -> np.ndarray | None
get_centroid(mask)        -> np.ndarray | None   shape (2,1) float32
init_kalman_filter(q_val, r_val) -> cv2.KalmanFilter
"""

from __future__ import annotations
import cv2
import numpy as np

# ── Morphology kernel sizes (large kernels → stable, compact ROI) ────────────
KERNEL_OPEN_SIZE  = (100, 100)
KERNEL_CLOSE_SIZE = (100, 100)
KERNEL_ERODE_SIZE = (100, 100)

# ── Default Kalman parameters ─────────────────────────────────────────────────
KALMAN_Q = 1e-8   # process noise  (smaller → smoother)
KALMAN_R = 15.0   # measurement noise (larger → smoother)


# ── Public helpers ────────────────────────────────────────────────────────────

def get_stable_mask(mask_raw: np.ndarray):
    """
    Apply open → close → erode to obtain a stable, compact core mask.

    Parameters
    ----------
    mask_raw : (H, W) uint8 binary mask

    Returns
    -------
    eroded mask (H, W) uint8, or None if input is None
    """
    if mask_raw is None:
        return None
    kernel_open  = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_OPEN_SIZE)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_CLOSE_SIZE)
    kernel_erode = cv2.getStructuringElement(cv2.MORPH_RECT, KERNEL_ERODE_SIZE)

    opened = cv2.morphologyEx(mask_raw,  cv2.MORPH_OPEN,  kernel_open)
    closed = cv2.morphologyEx(opened,    cv2.MORPH_CLOSE, kernel_close)
    eroded = cv2.morphologyEx(closed,    cv2.MORPH_ERODE, kernel_erode)
    return eroded


def get_centroid(mask: np.ndarray):
    """
    Compute the centroid of a binary mask.

    Returns
    -------
    np.ndarray shape (2,1) float32 with [[cx], [cy]], or None if mask is
    empty or None.
    """
    if mask is None:
        return None
    M = cv2.moments(mask)
    if M['m00'] == 0:
        return None
    cx = M['m10'] / M['m00']
    cy = M['m01'] / M['m00']
    return np.array([[np.float32(cx)], [np.float32(cy)]])


def init_kalman_filter(q_val: float = KALMAN_Q, r_val: float = KALMAN_R) -> cv2.KalmanFilter:
    """
    Create and return a 4-state (x, y, vx, vy) / 2-measurement (x, y)
    Kalman filter.

    Parameters
    ----------
    q_val : process noise covariance scalar
    r_val : measurement noise covariance scalar
    """
    kf = cv2.KalmanFilter(4, 2)
    kf.measurementMatrix = np.array(
        [[1, 0, 0, 0],
         [0, 1, 0, 0]], dtype=np.float32)
    kf.transitionMatrix = np.array(
        [[1, 0, 1, 0],
         [0, 1, 0, 1],
         [0, 0, 1, 0],
         [0, 0, 0, 1]], dtype=np.float32)
    kf.processNoiseCov     = np.eye(4, dtype=np.float32) * q_val
    kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * r_val
    return kf
