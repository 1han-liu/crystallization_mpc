"""Translation of gsensor/detection/update_line.m."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from crystallization_mpc.apps.gsensor.detection.calc_masked_image import calc_masked_image
from crystallization_mpc.apps.gsensor.detection.calc_theta_range import calc_theta_range
from crystallization_mpc.apps.gsensor.detection.find_edge_points_yolov import (
    find_edge_points_yolov,
)
from crystallization_mpc.apps.gsensor.utils.reorient_line import reorient_line

logger = logging.getLogger(__name__)


def update_line(image_file, params_G, line, t, e, n, o, is_opposite, kernel):
    old_line = line
    path = _image_file_path(image_file)
    logger.warning("update_line start: image=%s", path)
    I_orig = imread(path)
    logger.warning(
        "update_line image loaded: image=%s shape=%s dtype=%s",
        path,
        getattr(I_orig, "shape", None),
        getattr(I_orig, "dtype", None),
    )
    I = I_orig

    logger.warning("update_line YOLO edge detection start: image=%s", path)
    I = find_edge_points_yolov(I, kernel)
    logger.warning(
        "update_line YOLO edge detection done: image=%s edge_pixels=%s",
        path,
        int(np.count_nonzero(I)),
    )
    logger.warning("update_line mask start: image=%s", path)
    I, _ = calc_masked_image(
        I,
        line.point1,
        line.point2,
        n,
        _param(params_G, "width"),
        _param(params_G, "ratio"),
    )
    logger.warning(
        "update_line mask done: image=%s masked_edge_pixels=%s",
        path,
        int(np.count_nonzero(I)),
    )

    logger.warning("update_line hough start: image=%s", path)
    Hs, thetas, rhos = hough(I, theta=calc_theta_range(line.theta, _param(params_G, "delta_theta")))
    logger.warning(
        "update_line hough done: image=%s H_shape=%s theta_count=%s rho_count=%s",
        path,
        Hs.shape,
        len(thetas),
        len(rhos),
    )
    rho_min = line.rho - _param(params_G, "width") / _param(params_G, "width_divider")
    rho_max = line.rho + _param(params_G, "width") / _param(params_G, "width_divider")
    Hs[(rhos < rho_min) | (rhos > rho_max), :] = 0
    peaks = houghpeaks(Hs, _param(params_G, "num_peak"), nhood_size=(9, 1))
    logger.warning("update_line hough peaks done: image=%s peak_count=%s", path, len(peaks))
    lines = houghlines(I, thetas, rhos, peaks, fill_gap=5, min_length=7)
    logger.warning("update_line hough lines done: image=%s line_count=%s", path, len(lines))

    dist2o = np.inf
    line_cand = None
    for candidate in lines:
        candidate = reorient_line(candidate)
        candidate.point1 = _point3(candidate.point1)
        candidate.point2 = _point3(candidate.point2)
        if np.linalg.norm(candidate.point1 - candidate.point2) < _param(params_G, "len_min"):
            continue
        dist2o_new = np.dot(_point3(o) - _point3(line.point1), _point3(n)) * (
            float(is_opposite) - 0.5
        ) * 2
        if dist2o_new < dist2o:
            dist2o = dist2o_new
            line_cand = candidate

    if line_cand is None:
        line = old_line
    else:
        line = line_cand

    dist = abs(np.dot(_point3(line.point1) - _point3(t), _point3(n)))
    logger.warning("update_line done: image=%s dist=%s", path, dist)
    return line, dist, I_orig


def imread(path: str | Path):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for update_line imread support.") from exc

    with Image.open(path) as image:
        return np.asarray(image)


def hough(I, theta):
    I = np.asarray(I, dtype=bool)
    theta = np.asarray(theta, dtype=float).reshape(-1)
    height, width = I.shape[:2]
    diag = int(np.ceil(np.hypot(height, width)))
    rhos = np.arange(-diag, diag + 1, dtype=float)
    H = np.zeros((rhos.size, theta.size), dtype=float)
    y, x = np.nonzero(I)
    x = x.astype(float) + 1.0
    y = y.astype(float) + 1.0
    for theta_idx, theta_value in enumerate(theta):
        theta_rad = np.deg2rad(theta_value)
        rho_values = x * np.cos(theta_rad) + y * np.sin(theta_rad)
        rho_idx = np.rint(rho_values).astype(int) + diag
        valid = (rho_idx >= 0) & (rho_idx < rhos.size)
        np.add.at(H[:, theta_idx], rho_idx[valid], 1)
    return H, theta, rhos


def houghpeaks(H, num_peak, nhood_size=(9, 1)):
    H_work = np.asarray(H, dtype=float).copy()
    peaks = []
    num_peak = int(num_peak)
    row_radius = int(nhood_size[0]) // 2
    col_radius = int(nhood_size[1]) // 2
    for _ in range(num_peak):
        if H_work.size == 0 or np.max(H_work) <= 0:
            break
        row, col = np.unravel_index(np.argmax(H_work), H_work.shape)
        peaks.append((row, col))
        row_min = max(0, row - row_radius)
        row_max = min(H_work.shape[0], row + row_radius + 1)
        col_min = max(0, col - col_radius)
        col_max = min(H_work.shape[1], col + col_radius + 1)
        H_work[row_min:row_max, col_min:col_max] = 0
    return peaks


def houghlines(I, thetas, rhos, peaks, fill_gap=5, min_length=7):
    I = np.asarray(I, dtype=bool)
    y0, x0 = np.nonzero(I)
    if x0.size == 0:
        return []
    coords = np.column_stack([x0.astype(float) + 1.0, y0.astype(float) + 1.0])
    lines = []
    for rho_idx, theta_idx in peaks:
        theta_value = float(thetas[theta_idx])
        rho_value = float(rhos[rho_idx])
        theta_rad = np.deg2rad(theta_value)
        normal = np.array([np.cos(theta_rad), np.sin(theta_rad)])
        direction = np.array([-np.sin(theta_rad), np.cos(theta_rad)])
        distance = np.abs(coords @ normal - rho_value)
        selected = coords[distance <= 0.5]
        if selected.size == 0:
            continue
        projection = selected @ direction
        order = np.argsort(projection)
        selected = selected[order]
        projection = projection[order]
        for group in _projection_groups(selected, projection, fill_gap):
            if group.shape[0] < 2:
                continue
            point1 = group[0]
            point2 = group[-1]
            if np.linalg.norm(point1 - point2) < min_length:
                continue
            lines.append(
                SimpleNamespace(
                    point1=np.asarray(point1, dtype=float),
                    point2=np.asarray(point2, dtype=float),
                    theta=theta_value,
                    rho=rho_value,
                )
            )
    return lines


def _projection_groups(points: np.ndarray, projection: np.ndarray, fill_gap: float):
    start = 0
    for idx in range(1, projection.size):
        if projection[idx] - projection[idx - 1] > fill_gap:
            yield points[start:idx]
            start = idx
    yield points[start:]


def _image_file_path(image_file) -> Path:
    if isinstance(image_file, (str, Path)):
        return Path(image_file)
    if isinstance(image_file, dict):
        return Path(image_file["folder"]) / image_file["name"]
    return Path(image_file.folder) / image_file.name


def _param(params, key: str):
    if isinstance(params, dict):
        return params[key]
    return getattr(params, key)


def _point3(point: Any) -> np.ndarray:
    array = np.asarray(point, dtype=float).reshape(-1)
    if array.size == 2:
        array = np.array([array[0], array[1], 0.0])
    return array


__all__ = [
    "hough",
    "houghlines",
    "houghpeaks",
    "imread",
    "update_line",
]
