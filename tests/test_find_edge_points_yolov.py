import numpy as np

from crystallization_mpc.apps.gsensor.detection.find_edge_points_yolov import (
    IMGSZ,
    find_edge_points_yolov,
    letterbox,
    normalize_outputs,
    nms_xyxy_idx,
)


class _EmptyRunner:
    def run(self, x):
        assert x.shape == (1, 3, IMGSZ, IMGSZ)
        det = np.zeros((1, 37, 2), dtype=np.float32)
        proto = np.zeros((1, 32, 152, 152), dtype=np.float32)
        return det, proto


def test_letterbox_resizes_and_pads_to_target_shape():
    image = np.zeros((20, 10, 3), dtype=np.uint8)

    output, ratio, pad = letterbox(image, (608, 608))

    assert output.shape == (608, 608, 3)
    assert ratio == 608 / 20
    np.testing.assert_allclose(pad, [152.0, 0.0])
    assert output[0, 0, 0] == 114


def test_normalize_outputs_detects_det_and_proto_order():
    det = np.zeros((1, 37, 10), dtype=np.float32)
    proto = np.zeros((1, 32, 152, 152), dtype=np.float32)

    left_det, left_proto = normalize_outputs(det, proto)
    right_det, right_proto = normalize_outputs(proto, det)

    assert left_det is det
    assert left_proto is proto
    assert right_det is det
    assert right_proto is proto


def test_nms_xyxy_idx_keeps_highest_scored_overlapping_box():
    boxes = np.array(
        [
            [0.0, 0.0, 10.0, 10.0],
            [0.5, 0.5, 10.5, 10.5],
            [30.0, 30.0, 40.0, 40.0],
        ]
    )
    scores = np.array([0.9, 0.8, 0.7])

    keep = nms_xyxy_idx(boxes, scores, 0.75)

    np.testing.assert_array_equal(keep, [0, 2])


def test_find_edge_points_yolov_returns_empty_mask_without_detection():
    image = np.zeros((24, 32, 3), dtype=np.uint8)
    kernel = object()

    edge = find_edge_points_yolov(image, kernel, runner=_EmptyRunner())

    assert edge.shape == (24, 32)
    assert edge.dtype == np.bool_
    assert not np.any(edge)
