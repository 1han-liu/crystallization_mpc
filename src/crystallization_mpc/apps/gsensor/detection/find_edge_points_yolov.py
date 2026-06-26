"""Translation of gsensor/detection/find_edge_points_yolov.m."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull

from crystallization_mpc.apps.gsensor.detection.calc_kernel_mask import calc_kernel_mask

IMGSZ = 608
CONF_TH = 0.05
IOU_TH = 0.75
TOPK = 300
MODEL_PATH = Path(__file__).resolve().parents[1] / "model" / "best_1000_608.onnx"

_RUNNER = None


class YoloV8SegRunner(Protocol):
    def run(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        ...


class OnnxRuntimeYoloV8Seg:
    def __init__(self, model_path: str | Path = MODEL_PATH, providers: list[str] | None = None):
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime is required to run the YOLOv8 segmentation ONNX model."
            ) from exc

        self.model_path = Path(model_path)
        self.session = ort.InferenceSession(
            str(self.model_path),
            providers=providers or ["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [output.name for output in self.session.get_outputs()]

    def run(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        outputs = self.session.run(self.output_names, {self.input_name: x})
        if len(outputs) < 2:
            raise ValueError("YOLOv8 segmentation ONNX model must return det and proto outputs.")
        return outputs[0], outputs[1]


def get_default_runner() -> OnnxRuntimeYoloV8Seg:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = OnnxRuntimeYoloV8Seg(MODEL_PATH)
    return _RUNNER


def find_edge_points_yolov(I, kernel, runner: YoloV8SegRunner | None = None):
    runner = runner or get_default_runner()

    I0 = np.asarray(I)
    H0 = I0.shape[0]
    W0 = I0.shape[1]

    I_lb, _ratio, pad = letterbox(I0, (IMGSZ, IMGSZ))
    X = np.asarray(I_lb, dtype=np.float32) / 255.0
    X = np.transpose(X, (2, 0, 1))[np.newaxis, :, :, :]

    out1, out2 = runner.run(X)
    det, proto = normalize_outputs(out1, out2)
    det = _det_rows(det)
    proto = np.squeeze(proto)

    proto_c, proto_h, proto_w = proto.shape
    if proto_c != 32:
        raise ValueError(f"Unexpected proto channels: {proto_c}")
    P = np.reshape(np.transpose(proto, (1, 2, 0)), (-1, proto_c)).astype(np.float32)

    xywh = det[:, 0:4]
    score_r = det[:, 4]
    if np.all((score_r >= 0) & (score_r <= 1)):
        score = score_r
    else:
        score = _sigmoid(score_r)
    coeffs = det[:, 5:]

    keep = score >= CONF_TH
    xywh = xywh[keep, :]
    score = score[keep]
    coeffs = coeffs[keep, :]

    if score.size > TOPK:
        selected = np.argsort(score)[::-1][:TOPK]
        xywh = xywh[selected, :]
        score = score[selected]
        coeffs = coeffs[selected, :]

    if score.size == 0:
        return np.zeros((H0, W0), dtype=bool)

    xyxy = np.column_stack(
        [
            xywh[:, 0] - xywh[:, 2] / 2.0,
            xywh[:, 1] - xywh[:, 3] / 2.0,
            xywh[:, 0] + xywh[:, 2] / 2.0,
            xywh[:, 1] + xywh[:, 3] / 2.0,
        ]
    )
    xyxy[:, 0:2] = np.maximum(xyxy[:, 0:2], 0.0)
    xyxy[:, 2:4] = np.minimum(xyxy[:, 2:4], float(IMGSZ))

    idx = nms_xyxy_idx(xyxy, score, IOU_TH)
    xyxy = xyxy[idx, :]
    coeffs = coeffs[idx, :]

    padw = pad[0]
    padh = pad[1]
    mask_union = np.zeros((H0, W0), dtype=bool)

    for ii in range(xyxy.shape[0]):
        ci = coeffs[ii, :].astype(np.float32).reshape(-1, 1)
        m = _sigmoid((P @ ci).reshape(-1))
        m = np.reshape(m, (proto_h, proto_w))
        m = _resize_image(m, (IMGSZ, IMGSZ), order=1)

        x1 = max(0, int(np.floor(xyxy[ii, 0])))
        y1 = max(0, int(np.floor(xyxy[ii, 1])))
        x2 = min(IMGSZ, int(np.ceil(xyxy[ii, 2])))
        y2 = min(IMGSZ, int(np.ceil(xyxy[ii, 3])))
        mask_rsz = np.zeros((IMGSZ, IMGSZ), dtype=np.float32)
        mask_rsz[y1:y2, x1:x2] = m[y1:y2, x1:x2]

        x1p = int(np.floor(padw))
        y1p = int(np.floor(padh))
        x2p = IMGSZ - x1p
        y2p = IMGSZ - y1p
        mask_crop = mask_rsz[y1p:y2p, x1p:x2p]

        mask_ori = _resize_image(mask_crop, (H0, W0), order=1) > 0.50
        mask_ori = ndimage.binary_opening(mask_ori, structure=disk_strel(2))
        mask_lin = np.zeros_like(mask_ori, dtype=bool)
        for theta in range(0, 151, 30):
            mask_lin = mask_lin | ndimage.binary_closing(
                mask_ori,
                structure=line_strel(9, theta),
            )
        mask_ori = bwareaopen(mask_lin, 20)
        mask_union = mask_union | mask_ori

    mask_big = largest_component(mask_union)
    if not np.any(mask_big):
        return np.zeros((H0, W0), dtype=bool)

    boundary = bwperim(mask_big)
    y, x = np.nonzero(boundary)
    if x.size < 3:
        return np.zeros((H0, W0), dtype=bool)

    points = np.column_stack([x, y])
    hull = ConvexHull(points)
    hull_points = points[hull.vertices]
    hull_mask = poly2mask(hull_points[:, 0], hull_points[:, 1], H0, W0)

    edge_mask = bwperim(hull_mask)
    I_wo_kernel = edge_mask.copy()

    I_kernel = calc_kernel_mask(edge_mask, kernel)
    edge_mask[I_kernel] = True
    edge_mask = ndimage.binary_closing(edge_mask, structure=disk_strel(100))
    edge_mask = keep_largest_by_area_open(edge_mask)

    edge_mask = edge_mask & I_wo_kernel
    edge_mask = edge_nothinning(edge_mask)
    edge_mask = ndimage.binary_dilation(edge_mask, structure=disk_strel(3))
    return edge_mask.astype(bool)


def letterbox(im, new_shape: tuple[int, int]):
    im = ensure_rgb(np.asarray(im))
    h, w = im.shape[:2]
    r = min(new_shape[0] / h, new_shape[1] / w)
    nh = int(round(h * r))
    nw = int(round(w * r))
    img_resz = _resize_image(im, (nh, nw), order=1)
    dw = (new_shape[1] - nw) / 2.0
    dh = (new_shape[0] - nh) / 2.0
    pre_h = int(np.floor(dh))
    post_h = int(np.ceil(dh))
    pre_w = int(np.floor(dw))
    post_w = int(np.ceil(dw))
    img_lb = np.pad(
        img_resz,
        ((pre_h, post_h), (pre_w, post_w), (0, 0)),
        mode="constant",
        constant_values=114,
    )
    return img_lb, r, np.array([dw, dh], dtype=float)


def normalize_outputs(a, b):
    if _is_det_output(a):
        return a, b
    if _is_det_output(b):
        return b, a
    raise ValueError("Unable to identify det/proto outputs.")


def nms_xyxy_idx(boxes, scores, iou_th):
    boxes = np.asarray(boxes, dtype=float)
    scores = np.asarray(scores, dtype=float).reshape(-1)
    if boxes.shape[0] == 0:
        return np.zeros((0,), dtype=int)
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    w = np.maximum(0.0, x2 - x1)
    h = np.maximum(0.0, y2 - y1)
    area = w * h
    order = np.argsort(scores)[::-1]
    keep = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        ww = np.maximum(0.0, xx2 - xx1)
        hh = np.maximum(0.0, yy2 - yy1)
        inter = ww * hh
        iou = inter / (area[i] + area[rest] - inter + 1.0e-9)
        order = rest[iou <= iou_th]
    return np.asarray(keep, dtype=int)


def safe_take(xyxy, score, coeffs, idx):
    if np.asarray(idx).dtype == bool:
        idx = np.flatnonzero(idx)
    idx = np.asarray(idx).reshape(-1)
    if idx.size == 0:
        return (
            np.zeros((0, 4), dtype=np.asarray(xyxy).dtype),
            np.zeros((0,), dtype=np.asarray(score).dtype),
            np.zeros((0, np.asarray(coeffs).shape[1]), dtype=np.asarray(coeffs).dtype),
        )
    idx = np.rint(idx).astype(int)
    idx = idx[(idx >= 0) & (idx < np.asarray(xyxy).shape[0])]
    _, unique_pos = np.unique(idx, return_index=True)
    idx = idx[np.sort(unique_pos)]
    return np.asarray(xyxy)[idx, :], np.asarray(score)[idx], np.asarray(coeffs)[idx, :]


def overlay_tint(img, mask, rgb, alpha):
    img = np.asarray(img, dtype=np.uint8)
    mask = np.asarray(mask, dtype=bool)
    tint = np.zeros_like(img, dtype=np.uint8)
    tint[:, :, 0] = rgb[0]
    tint[:, :, 1] = rgb[1]
    tint[:, :, 2] = rgb[2]
    out = img.copy()
    if np.any(mask):
        idx = np.repeat(mask[:, :, np.newaxis], 3, axis=2)
        out[idx] = (alpha * tint[idx] + (1.0 - alpha) * img[idx]).astype(np.uint8)
    return out


def ensure_rgb(im):
    if im.ndim == 2:
        return np.repeat(im[:, :, np.newaxis], 3, axis=2)
    if im.shape[2] == 1:
        return np.repeat(im, 3, axis=2)
    return im[:, :, :3]


def _det_rows(det):
    det = np.squeeze(np.asarray(det, dtype=np.float32))
    if det.ndim != 2:
        raise ValueError(f"Unexpected det output shape: {det.shape}")
    if det.shape[0] == 37:
        return det.T
    if det.shape[1] == 37:
        return det
    raise ValueError(f"Unexpected det output shape: {det.shape}")


def _is_det_output(value):
    shape = np.asarray(value).shape
    return len(shape) >= 3 and shape[1] == 37


def _resize_image(image, shape: tuple[int, int], order: int):
    image = np.asarray(image)
    factors = [shape[0] / image.shape[0], shape[1] / image.shape[1]]
    if image.ndim == 3:
        factors.append(1.0)
    return ndimage.zoom(image, factors, order=order)


def _sigmoid(value):
    return 1.0 / (1.0 + np.exp(-value))


def disk_strel(radius: int):
    y, x = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return x * x + y * y <= radius * radius


def line_strel(length: int, theta_deg: float):
    size = int(length)
    center = (size - 1) / 2.0
    yy, xx = np.mgrid[0:size, 0:size]
    theta = np.deg2rad(theta_deg)
    dx = np.cos(theta)
    dy = -np.sin(theta)
    distance = np.abs((xx - center) * dy - (yy - center) * dx)
    projection = (xx - center) * dx + (yy - center) * dy
    return (distance <= 0.5) & (np.abs(projection) <= center)


def bwareaopen(mask, area_threshold: int):
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return np.zeros_like(mask, dtype=bool)
    areas = np.bincount(labels.ravel())
    keep = areas >= area_threshold
    keep[0] = False
    return keep[labels]


def largest_component(mask):
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return np.zeros_like(mask, dtype=bool)
    areas = np.bincount(labels.ravel())
    areas[0] = 0
    return labels == int(np.argmax(areas))


def keep_largest_by_area_open(mask):
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return np.zeros_like(mask, dtype=bool)
    areas = np.bincount(labels.ravel())
    areas[0] = 0
    area = int(np.max(areas))
    return bwareaopen(mask, area)


def bwperim(mask):
    mask = np.asarray(mask, dtype=bool)
    eroded = ndimage.binary_erosion(mask, structure=np.ones((3, 3), dtype=bool))
    return mask & ~eroded


def poly2mask(x, y, height: int, width: int):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    yy, xx = np.mgrid[0:height, 0:width]
    inside = np.zeros((height, width), dtype=bool)
    j = len(x) - 1
    for i in range(len(x)):
        intersects = ((y[i] > yy) != (y[j] > yy)) & (
            xx < (x[j] - x[i]) * (yy - y[i]) / (y[j] - y[i] + 1.0e-12) + x[i]
        )
        inside ^= intersects
        j = i
    return inside


def edge_nothinning(mask):
    mask = np.asarray(mask, dtype=bool)
    dilated = ndimage.binary_dilation(mask, structure=np.ones((3, 3), dtype=bool))
    eroded = ndimage.binary_erosion(mask, structure=np.ones((3, 3), dtype=bool))
    return dilated & ~eroded


__all__ = [
    "CONF_TH",
    "IMGSZ",
    "IOU_TH",
    "MODEL_PATH",
    "OnnxRuntimeYoloV8Seg",
    "TOPK",
    "find_edge_points_yolov",
    "get_default_runner",
    "letterbox",
    "normalize_outputs",
    "nms_xyxy_idx",
    "overlay_tint",
    "safe_take",
]
