"""Translation of gsensor/detection/update_line.m."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from crystallization_mpc.apps.gsensor.detection.calc_masked_image import calc_masked_image
from crystallization_mpc.apps.gsensor.detection.find_edge_points_yolov import (
    find_edge_points_yolov,
)
from crystallization_mpc.apps.gsensor.utils.reorient_line import reorient_line

logger = logging.getLogger(__name__)


def update_line(
    image_file,
    params_G,
    line,
    t,
    e,
    n,
    o,
    is_opposite,
    kernel,
    *,
    debug_dir: str | Path | None = None,
    debug_label: str | None = None,
):
    old_line = line
    path = _image_file_path(image_file)
    debug_label = debug_label or path.stem
    logger.warning("update_line start: image=%s", path)
    I_orig = imread(path)
    logger.warning(
        "update_line image loaded: image=%s shape=%s dtype=%s",
        path,
        getattr(I_orig, "shape", None),
        getattr(I_orig, "dtype", None),
    )
    I = I_orig
    _save_debug_image(
        debug_dir,
        debug_label,
        "01_original_old_line",
        I_orig,
        lines=[old_line],
        line_fill=(255, 215, 0),
    )

    logger.warning("update_line YOLO edge detection start: image=%s", path)
    I = find_edge_points_yolov(I, kernel)
    logger.warning(
        "update_line YOLO edge detection done: image=%s edge_pixels=%s",
        path,
        int(np.count_nonzero(I)),
    )
    _save_debug_image(debug_dir, debug_label, "02_edges", I)
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
    _save_debug_image(
        debug_dir,
        debug_label,
        "03_masked_edges",
        I,
        lines=[old_line],
        line_fill=(255, 215, 0),
    )

    logger.warning("update_line OpenCV hough start: image=%s", path)
    lines = houghlines_opencv(
        I,
        line,
        params_G,
        debug_dir=debug_dir,
        debug_label=debug_label,
        debug_base_image=I_orig,
    )
    logger.warning("update_line OpenCV hough done: image=%s line_count=%s", path, len(lines))

    dist2o = np.inf
    line_cand = None
    len_min = _param(params_G, "len_min")
    # Candidate diagnostics split the zero-growth case into three possibilities:
    # Hough found no usable line, filtering rejected candidates, or projection gave zero.
    logger.warning(
        "update_line candidate scan start: image=%s line_count=%s len_min=%s "
        "old_line_p1=%s old_line_p2=%s old_theta=%s old_rho=%s t=%s n=%s o=%s "
        "is_opposite=%s",
        path,
        len(lines),
        len_min,
        _point3(line.point1).tolist(),
        _point3(line.point2).tolist(),
        getattr(line, "theta", None),
        getattr(line, "rho", None),
        _point3(t).tolist(),
        _point3(n).tolist(),
        _point3(o).tolist(),
        is_opposite,
    )
    for candidate_index, candidate in enumerate(lines):
        candidate = reorient_line(candidate)
        candidate.point1 = _point3(candidate.point1)
        candidate.point2 = _point3(candidate.point2)
        candidate_length = float(np.linalg.norm(candidate.point1 - candidate.point2))
        logger.warning(
            "update_line candidate: image=%s index=%s p1=%s p2=%s length=%s "
            "theta=%s rho=%s",
            path,
            candidate_index,
            candidate.point1.tolist(),
            candidate.point2.tolist(),
            candidate_length,
            getattr(candidate, "theta", None),
            getattr(candidate, "rho", None),
        )
        if candidate_length < len_min:
            logger.warning(
                "update_line candidate skipped by len_min: image=%s index=%s "
                "length=%s len_min=%s",
                path,
                candidate_index,
                candidate_length,
                len_min,
            )
            continue
        # The MATLAB source uses ``line.point1`` here, which gives every
        # candidate the same score.  Scoring the candidate itself preserves the
        # intended inward/outward boundary choice.
        dist2o_new = np.dot(_point3(o) - candidate.point1, _point3(n)) * (
            float(is_opposite) - 0.5
        ) * 2
        logger.warning(
            "update_line candidate score: image=%s index=%s dist2o_new=%s "
            "current_best=%s",
            path,
            candidate_index,
            dist2o_new,
            dist2o,
        )
        if dist2o_new < dist2o:
            dist2o = dist2o_new
            line_cand = candidate
            logger.warning(
                "update_line candidate selected as best: image=%s index=%s "
                "dist2o=%s",
                path,
                candidate_index,
                dist2o,
            )

    if line_cand is None:
        logger.warning("update_line candidate scan done: image=%s selected=old_line", path)
        line = old_line
    else:
        logger.warning(
            "update_line candidate scan done: image=%s selected=new_line p1=%s p2=%s "
            "theta=%s rho=%s",
            path,
            line_cand.point1.tolist(),
            line_cand.point2.tolist(),
            getattr(line_cand, "theta", None),
            getattr(line_cand, "rho", None),
        )
        line = line_cand

    # The fallback line keeps tracking geometry continuous, but callers still
    # need to know that this frame did not contain a valid Hough measurement.
    line.detection_valid = line_cand is not None

    _save_debug_image(
        debug_dir,
        debug_label,
        "06_selected_line",
        I_orig,
        lines=[line],
        line_fill=(0, 255, 255) if line_cand is not None else (255, 215, 0),
        title=(
            "selected=new_line"
            if line_cand is not None
            else "selected=old_line; no valid Hough candidate"
        ),
    )

    dist = abs(np.dot(_point3(line.point1) - _point3(t), _point3(n)))
    logger.warning(
        "update_line distance projection: image=%s line_p1=%s t=%s n=%s dist=%s",
        path,
        _point3(line.point1).tolist(),
        _point3(t).tolist(),
        _point3(n).tolist(),
        dist,
    )
    logger.warning("update_line done: image=%s dist=%s", path, dist)
    return line, dist, I_orig


def imread(path: str | Path):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for update_line imread support.") from exc

    with Image.open(path) as image:
        return np.asarray(image)


def houghlines_opencv(
    I,
    line,
    params_G,
    *,
    cv2_module=None,
    debug_dir: str | Path | None = None,
    debug_label: str | None = None,
    debug_base_image: Any = None,
):
    """Detect growth-line candidates with OpenCV's probabilistic Hough transform."""

    image = _opencv_hough_image(I)
    edge_pixels = int(np.count_nonzero(image))
    if not np.any(image):
        logger.warning("OpenCV hough skipped: edge_pixels=0")
        return []

    cv2 = cv2_module or _import_cv2()
    threshold = int(_param(params_G, "hough_threshold", 10))
    rho_resolution = float(_param(params_G, "hough_rho_resolution", 1.0))
    theta_resolution_deg = float(_param(params_G, "hough_theta_resolution_deg", 1.0))
    min_line_length = float(_param(params_G, "hough_min_line_length", 7.0))
    max_line_gap = float(_param(params_G, "hough_max_line_gap", 5.0))
    width = float(_param(params_G, "width"))
    preferred_rho_half_width = width / float(_param(params_G, "width_divider"))
    recovery_rho_half_width = width
    rho_min = line.rho - preferred_rho_half_width
    rho_max = line.rho + preferred_rho_half_width
    recovery_rho_min = line.rho - recovery_rho_half_width
    recovery_rho_max = line.rho + recovery_rho_half_width
    delta_theta = float(_param(params_G, "delta_theta"))
    len_min = float(_param(params_G, "len_min"))
    max_candidates = int(_param(params_G, "hough_max_candidates", _param(params_G, "num_peak", 0)))

    logger.warning(
        "OpenCV hough params: edge_pixels=%s threshold=%s rho_resolution=%s "
        "theta_resolution_deg=%s min_line_length=%s max_line_gap=%s "
        "theta_center=%s delta_theta=%s rho_center=%s rho_min=%s rho_max=%s "
        "recovery_rho_min=%s recovery_rho_max=%s len_min=%s max_candidates=%s",
        edge_pixels,
        threshold,
        rho_resolution,
        theta_resolution_deg,
        min_line_length,
        max_line_gap,
        getattr(line, "theta", None),
        delta_theta,
        getattr(line, "rho", None),
        rho_min,
        rho_max,
        recovery_rho_min,
        recovery_rho_max,
        len_min,
        max_candidates,
    )

    raw_lines = cv2.HoughLinesP(
        image,
        rho=rho_resolution,
        theta=np.deg2rad(theta_resolution_deg),
        threshold=threshold,
        minLineLength=min_line_length,
        maxLineGap=max_line_gap,
    )
    if raw_lines is None:
        _save_debug_image(
            debug_dir,
            debug_label,
            "04_raw_lines",
            debug_base_image if debug_base_image is not None else I,
            title="raw_line_count=0",
        )
        _save_debug_image(
            debug_dir,
            debug_label,
            "05_accepted_lines",
            debug_base_image if debug_base_image is not None else I,
            title="accepted_line_count=0",
        )
        logger.warning(
            "OpenCV hough diagnostics: edge_pixels=%s raw_line_count=0 "
            "theta_pass_count=0 rho_pass_count=0 final_line_count=0",
            edge_pixels,
        )
        return []

    raw_segments = np.asarray(raw_lines, dtype=float).reshape(-1, 4)
    _save_debug_image(
        debug_dir,
        debug_label,
        "04_raw_lines",
        debug_base_image if debug_base_image is not None else I,
        segments=raw_segments,
        line_fill=(255, 0, 0),
        title=f"raw_line_count={raw_segments.shape[0]}",
    )

    raw_line_count = 0
    theta_pass_count = 0
    rho_pass_count = 0
    preferred_candidates = []
    recovery_candidates = []
    for segment in raw_segments:
        raw_line_count += 1
        candidate = _line_from_opencv_segment(segment, reference_theta=float(line.theta))
        if candidate is None:
            continue
        theta_error = _theta_distance_deg(candidate.theta, line.theta)
        rho_error = abs(float(candidate.rho) - float(line.rho))
        reject_reason = "accepted"
        if theta_error > delta_theta:
            reject_reason = "theta"
            if raw_line_count <= 20:
                logger.warning(
                    "OpenCV hough raw: index=%s p1=%s p2=%s theta=%s rho=%s "
                    "theta_error=%s rho_error=%s reject=%s",
                    raw_line_count - 1,
                    np.asarray(candidate.point1, dtype=float).tolist(),
                    np.asarray(candidate.point2, dtype=float).tolist(),
                    getattr(candidate, "theta", None),
                    getattr(candidate, "rho", None),
                    theta_error,
                    rho_error,
                    reject_reason,
                )
            continue
        theta_pass_count += 1
        length = float(np.linalg.norm(candidate.point1 - candidate.point2))
        if length < len_min:
            reject_reason = "length"
            if raw_line_count <= 20:
                logger.warning(
                    "OpenCV hough raw: index=%s p1=%s p2=%s theta=%s rho=%s "
                    "theta_error=%s rho_error=%s reject=%s length=%s len_min=%s",
                    raw_line_count - 1,
                    np.asarray(candidate.point1, dtype=float).tolist(),
                    np.asarray(candidate.point2, dtype=float).tolist(),
                    getattr(candidate, "theta", None),
                    getattr(candidate, "rho", None),
                    theta_error,
                    rho_error,
                    reject_reason,
                    length,
                    len_min,
                )
            continue
        if candidate.rho < recovery_rho_min or candidate.rho > recovery_rho_max:
            reject_reason = "rho"
            if raw_line_count <= 20:
                logger.warning(
                    "OpenCV hough raw: index=%s p1=%s p2=%s theta=%s rho=%s "
                    "theta_error=%s rho_error=%s reject=%s",
                    raw_line_count - 1,
                    np.asarray(candidate.point1, dtype=float).tolist(),
                    np.asarray(candidate.point2, dtype=float).tolist(),
                    getattr(candidate, "theta", None),
                    getattr(candidate, "rho", None),
                    theta_error,
                    rho_error,
                    reject_reason,
                )
            continue
        candidate = _extend_candidate_to_reference_span(candidate, line)
        if rho_min <= candidate.rho <= rho_max:
            rho_pass_count += 1
            preferred_candidates.append((length, theta_error, rho_error, candidate))
            search_band = "preferred"
        else:
            recovery_candidates.append((length, theta_error, rho_error, candidate))
            search_band = "recovery"
        if raw_line_count <= 20:
            logger.warning(
                "OpenCV hough raw: index=%s p1=%s p2=%s theta=%s rho=%s "
                "theta_error=%s rho_error=%s reject=%s search_band=%s length=%s",
                raw_line_count - 1,
                np.asarray(candidate.point1, dtype=float).tolist(),
                np.asarray(candidate.point2, dtype=float).tolist(),
                getattr(candidate, "theta", None),
                getattr(candidate, "rho", None),
                theta_error,
                rho_error,
                reject_reason,
                search_band,
                length,
            )

    recovery_used = not preferred_candidates and bool(recovery_candidates)
    candidates = preferred_candidates if preferred_candidates else recovery_candidates
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    lines = [candidate for *_metrics, candidate in candidates]
    if max_candidates > 0:
        lines = lines[:max_candidates]
    _save_debug_image(
        debug_dir,
        debug_label,
        "05_accepted_lines",
        debug_base_image if debug_base_image is not None else I,
        lines=lines,
        line_fill=(0, 255, 0),
        title=(
            f"accepted_line_count={len(lines)}; "
            f"search_band={'recovery' if recovery_used else 'preferred'}"
        ),
    )
    logger.warning(
        "OpenCV hough diagnostics: edge_pixels=%s raw_line_count=%s "
        "theta_pass_count=%s rho_pass_count=%s recovery_candidate_count=%s "
        "recovery_used=%s final_line_count=%s",
        edge_pixels,
        raw_line_count,
        theta_pass_count,
        rho_pass_count,
        len(recovery_candidates),
        recovery_used,
        len(lines),
    )
    for index, candidate in enumerate(lines[:10]):
        logger.warning(
            "OpenCV hough accepted: index=%s p1=%s p2=%s theta=%s rho=%s length=%s",
            index,
            np.asarray(candidate.point1, dtype=float).tolist(),
            np.asarray(candidate.point2, dtype=float).tolist(),
            getattr(candidate, "theta", None),
            getattr(candidate, "rho", None),
            float(np.linalg.norm(candidate.point1 - candidate.point2)),
        )
    return lines


def _import_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "opencv-python-headless is required for update_line Hough support."
        ) from exc
    return cv2


def _opencv_hough_image(I) -> np.ndarray:
    image = np.asarray(I)
    if image.ndim == 3:
        image = np.any(image != 0, axis=2)
    else:
        image = image != 0
    return image.astype(np.uint8) * 255


def _line_from_opencv_segment(segment, *, reference_theta: float | None = None):
    x1, y1, x2, y2 = np.asarray(segment, dtype=float).reshape(4)
    point1 = np.array([x1, y1], dtype=float)
    point2 = np.array([x2, y2], dtype=float)
    theta_rho = _theta_rho_from_points(point1, point2)
    if theta_rho is None:
        return None
    theta, rho = theta_rho
    if reference_theta is not None:
        theta, rho = _orient_theta_rho_to_reference(theta, rho, reference_theta)
    return SimpleNamespace(point1=point1, point2=point2, theta=theta, rho=rho)


def _theta_rho_from_points(point1: np.ndarray, point2: np.ndarray):
    direction = np.asarray(point2, dtype=float)[:2] - np.asarray(point1, dtype=float)[:2]
    length = float(np.linalg.norm(direction))
    if length <= 0:
        return None
    normal = np.array([direction[1], -direction[0]], dtype=float) / length
    theta = float(np.degrees(np.arctan2(normal[1], normal[0])))
    rho = float(np.dot(normal, np.asarray(point1, dtype=float)[:2]))
    return _normalize_theta_rho(theta, rho)


def _normalize_theta_rho(theta: float, rho: float) -> tuple[float, float]:
    while theta >= 90.0:
        theta -= 180.0
        rho = -rho
    while theta < -90.0:
        theta += 180.0
        rho = -rho
    return theta, rho


def _theta_distance_deg(theta: float, reference: float) -> float:
    return abs((float(theta) - float(reference) + 90.0) % 180.0 - 90.0)


def _orient_theta_rho_to_reference(
    theta: float,
    rho: float,
    reference_theta: float,
) -> tuple[float, float]:
    """Choose the equivalent normal direction used by the tracked line."""

    normal = np.array(
        [np.cos(np.deg2rad(theta)), np.sin(np.deg2rad(theta))],
        dtype=float,
    )
    reference_normal = np.array(
        [np.cos(np.deg2rad(reference_theta)), np.sin(np.deg2rad(reference_theta))],
        dtype=float,
    )
    if float(np.dot(normal, reference_normal)) < 0.0:
        theta += 180.0
        rho = -rho
    return float(theta), float(rho)


def _extend_candidate_to_reference_span(candidate: Any, reference_line: Any):
    """Draw the detected infinite line across the initialized edge span.

    ``HoughLinesP`` often returns only a short visible fragment near a corner.
    The fragment determines theta/rho, while projecting the previous endpoints
    onto that line keeps the final u/v overlay continuous and comparable from
    frame to frame.
    """

    normal = np.array(
        [
            np.cos(np.deg2rad(float(candidate.theta))),
            np.sin(np.deg2rad(float(candidate.theta))),
        ],
        dtype=float,
    )

    def project(point: Any) -> np.ndarray:
        point_2d = np.asarray(point, dtype=float).reshape(-1)[:2]
        correction = float(candidate.rho) - float(np.dot(normal, point_2d))
        return point_2d + correction * normal

    candidate.point1 = project(reference_line.point1)
    candidate.point2 = project(reference_line.point2)
    return candidate


def _save_debug_image(
    debug_dir: str | Path | None,
    label: str | None,
    stage: str,
    image: Any,
    *,
    lines: list[Any] | None = None,
    segments: np.ndarray | None = None,
    line_fill: tuple[int, int, int] = (255, 0, 0),
    title: str | None = None,
) -> None:
    if debug_dir is None:
        return

    try:
        from PIL import ImageDraw
    except ImportError:
        logger.warning("update_line debug image skipped: Pillow is not available")
        return

    output_dir = Path(debug_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_label = _safe_debug_label(label or "frame")
    output_path = output_dir / f"{safe_label}_{stage}.png"
    canvas = _debug_rgb_image(image)
    draw = ImageDraw.Draw(canvas)

    if title:
        draw.rectangle([0, 0, min(canvas.width, 900), 24], fill=(0, 0, 0))
        draw.text((6, 5), title, fill=(255, 255, 255))

    for segment in [] if segments is None else np.asarray(segments, dtype=float).reshape(-1, 4):
        x1, y1, x2, y2 = segment
        draw.line([(float(x1), float(y1)), (float(x2), float(y2))], fill=line_fill, width=2)

    for candidate in lines or []:
        try:
            draw.line(
                [_xy(candidate.point1), _xy(candidate.point2)],
                fill=line_fill,
                width=3,
            )
        except Exception:
            logger.exception("update_line debug image skipped invalid line")

    canvas.save(output_path)
    logger.warning("update_line debug image saved: %s", output_path)


def _debug_rgb_image(image: Any):
    from PIL import Image

    array = np.asarray(image)
    if array.dtype == bool:
        array = array.astype(np.uint8) * 255
    elif np.issubdtype(array.dtype, np.floating):
        scale = 255.0 if array.size and np.nanmax(array) <= 1.0 else 1.0
        array = np.clip(array * scale, 0, 255).astype(np.uint8)
    elif array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)

    if array.ndim == 2:
        return Image.fromarray(array).convert("RGB")
    if array.ndim == 3 and array.shape[2] == 1:
        return Image.fromarray(array[:, :, 0]).convert("RGB")
    if array.ndim == 3 and array.shape[2] >= 3:
        return Image.fromarray(array[:, :, :3]).convert("RGB")
    raise ValueError(f"Unsupported debug image shape: {array.shape}")


def _safe_debug_label(label: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in label)


def _xy(point: Any) -> tuple[float, float]:
    array = np.asarray(point, dtype=float).reshape(-1)
    if array.size < 2:
        raise ValueError("Line points must contain at least x and y.")
    return float(array[0]), float(array[1])


def _image_file_path(image_file) -> Path:
    if isinstance(image_file, (str, Path)):
        return Path(image_file)
    if isinstance(image_file, dict):
        return Path(image_file["folder"]) / image_file["name"]
    return Path(image_file.folder) / image_file.name


_MISSING = object()


def _param(params, key: str, default: Any = _MISSING):
    if isinstance(params, dict):
        if key in params:
            return params[key]
    elif hasattr(params, key):
        return getattr(params, key)
    if default is _MISSING:
        raise KeyError(key)
    return default


def _point3(point: Any) -> np.ndarray:
    array = np.asarray(point, dtype=float).reshape(-1)
    if array.size == 2:
        array = np.array([array[0], array[1], 0.0])
    return array


__all__ = [
    "houghlines_opencv",
    "imread",
    "update_line",
]
