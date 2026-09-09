from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from crystallization_mpc.apps.central.params import load_params
from crystallization_mpc.apps.gsensor.detection.find_edge_points_yolov import (
    CrystalSegmentation,
    edge_points_from_measurement_mask,
    find_edge_points_yolov,
    segment_crystal_yolov,
)
from crystallization_mpc.apps.gsensor.growth_rate_processor import GrowthRateProcessor
from crystallization_mpc.apps.gsensor.alignment import (
    ALIGNMENT_METHODS,
    AlignmentInput,
    AlignmentDiagnostics,
    AlignmentResult,
)


def _params() -> dict[str, object]:
    shared, gsensor, _controller, _version = load_params("params_default.yaml")
    return {**shared, **gsensor}


def _uv() -> SimpleNamespace:
    return SimpleNamespace(
        t=np.array([20.0, 20.0, 0.0]),
        e=np.array([80.0, 20.0, 0.0]),
        n=np.array([0.0, 1.0, 0.0]),
        o=np.array([50.0, 50.0, 0.0]),
        is_opposite=False,
        theta_0=0.0,
        rho_0=20.0,
    )


def test_edge_builder_accepts_reusable_measurement_mask() -> None:
    mask = np.zeros((256, 256), dtype=bool)
    mask[40:220, 50:210] = True
    original = mask.copy()
    kernel = SimpleNamespace(k_c_cell=[], k_o_cell=[])
    edge = edge_points_from_measurement_mask(mask, kernel=kernel)
    assert edge.shape == mask.shape
    assert edge.dtype == bool
    assert np.array_equal(mask, original)


def test_none_split_pipeline_matches_compatibility_wrapper() -> None:
    class Runner:
        def __init__(self):
            self.calls = 0

        def run(self, _image):
            self.calls += 1
            detections = np.zeros((1, 37, 2), dtype=np.float32)
            detections[0, 0:4, 0] = [304, 304, 320, 320]
            detections[0, 4, 0] = 0.9
            detections[0, 5, 0] = 10.0
            prototypes = np.zeros((1, 32, 152, 152), dtype=np.float32)
            prototypes[0, 0] = 1.0
            return detections, prototypes

    image = np.full((256, 320, 3), 100, dtype=np.uint8)
    kernel = SimpleNamespace(k_c_cell=[], k_o_cell=[])
    wrapper_runner = Runner()
    expected = find_edge_points_yolov(image, kernel, runner=wrapper_runner)
    split_runner = Runner()
    segmentation = segment_crystal_yolov(image, runner=split_runner)
    actual = edge_points_from_measurement_mask(segmentation.measurement_mask, kernel)
    assert np.array_equal(actual, expected)
    assert wrapper_runner.calls == 1
    assert split_runner.calls == 1


def test_processor_segments_once_and_reuses_edges_for_both_directions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import crystallization_mpc.apps.gsensor.growth_rate_processor as module

    image_path = tmp_path / "frame.png"
    Image.fromarray(np.full((128, 128, 3), 120, dtype=np.uint8)).save(image_path)
    calls = {"segment": 0, "edge": 0, "update": 0}
    mask = np.zeros((128, 128), dtype=bool)
    mask[20:110, 20:110] = True

    def fake_segment(_image, runner=None):
        calls["segment"] += 1
        return CrystalSegmentation(mask.copy(), mask.copy())

    def fake_edge(_mask, _kernel):
        calls["edge"] += 1
        return mask.copy()

    def fake_update(uv, _path, ii, _params_g, _kernel, **kwargs):
        calls["update"] += 1
        uv.line.detection_valid = True
        uv.dist_array.append(float(ii))
        return uv, kwargs["original_image"]

    def fake_ekf(uv, _dt, _resolution, ii):
        uv.distance_array.append(float(ii))
        uv.distance_KF_array.append(float(ii))
        uv.G_array.append(float(ii))
        uv.G_KF_array.append(float(ii))
        uv.x_G_array = np.zeros((3, ii), dtype=float)
        return uv

    monkeypatch.setattr(module, "segment_crystal_yolov", fake_segment)
    monkeypatch.setattr(module, "edge_points_from_measurement_mask", fake_edge)
    monkeypatch.setattr(module, "update_uv_struct", fake_update)
    monkeypatch.setattr(module, "update_EKF_G", fake_ekf)
    monkeypatch.setattr(
        GrowthRateProcessor,
        "_write_overlay",
        lambda self, *args, **kwargs: tmp_path / "overlay.jpg",
    )

    processor = GrowthRateProcessor(
        run_id="test",
        params=_params(),
        uv_struct_list=[_uv(), _uv()],
        kernel=np.array([[0, 0], [1, 1]]),
        latest_overlay_path=tmp_path / "latest.jpg",
    )
    result = processor.process(image_path)

    assert result.valid
    assert result.alignment is not None and result.alignment.method == "none"
    assert calls == {"segment": 1, "edge": 1, "update": 2}


@pytest.mark.parametrize("method", ALIGNMENT_METHODS)
def test_processor_alignment_state_round_trip(tmp_path: Path, method: str) -> None:
    first = GrowthRateProcessor(
        run_id="test",
        params={**_params(), "alignment_method": method},
        uv_struct_list=[_uv(), _uv()],
        kernel=np.array([[0, 0], [1, 1]]),
        latest_overlay_path=tmp_path / "latest.jpg",
        initial_image_path=tmp_path / "baseline.png",
    )
    mask = np.zeros((512, 512), dtype=np.uint8)
    mask[60:460, 60:460] = 255
    first.aligner.initialize(AlignmentInput(mask, mask, mask))
    first.aligner_initialized = True
    state = first.export_state()
    assert state["schema_version"] == 1
    assert state["alignment"]["method"] == method
    assert (tmp_path / "alignment_state.npz").is_file()

    restored = GrowthRateProcessor(
        run_id="test",
        params={**_params(), "alignment_method": method},
        uv_struct_list=[_uv(), _uv()],
        kernel=np.array([[0, 0], [1, 1]]),
        latest_overlay_path=tmp_path / "latest.jpg",
        initial_image_path=tmp_path / "baseline.png",
    )
    restored.restore_state(state)
    assert restored.aligner_initialized
    assert restored.alignment_method == method
    # Persisted reference arrays and method-specific history survive the round trip.
    def equal(left, right):
        if isinstance(left, np.ndarray):
            np.testing.assert_array_equal(left, right)
        elif isinstance(left, dict):
            assert left.keys() == right.keys()
            for key in left:
                equal(left[key], right[key])
        elif isinstance(left, (list, tuple)):
            assert len(left) == len(right)
            for a, b in zip(left, right):
                equal(a, b)
        else:
            assert left == right
    equal(first.aligner.export_state(), restored.aligner.export_state())
    # For non-neural methods, also compare the next actual alignment after restore.
    if method != "loftr":
        import cv2
        cv2.setRNGSeed(0)
        expected = first.aligner.align(AlignmentInput(mask, mask, mask))
        cv2.setRNGSeed(0)
        actual = restored.aligner.align(AlignmentInput(mask, mask, mask))
        np.testing.assert_allclose(actual.transform, expected.transform)
        np.testing.assert_array_equal(actual.aligned_measurement_mask, expected.aligned_measurement_mask)
        assert actual.diagnostics.success == expected.diagnostics.success


def test_alignment_failure_rolls_back_alignment_and_measurement_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import crystallization_mpc.apps.gsensor.growth_rate_processor as module

    image_path = tmp_path / "frame.png"
    Image.fromarray(np.full((32, 32, 3), 120, dtype=np.uint8)).save(image_path)
    mask = np.ones((32, 32), dtype=bool)
    monkeypatch.setattr(
        module,
        "segment_crystal_yolov",
        lambda _image, runner=None: CrystalSegmentation(mask, mask),
    )

    class FailingAligner:
        def __init__(self):
            self.counter = 0

        def align(self, frame):
            self.counter += 1
            return AlignmentResult(
                frame.image_gray,
                frame.measurement_mask,
                np.eye(2, 3, dtype=np.float32),
                AlignmentDiagnostics(
                    method="fft",
                    success=False,
                    fallback_used=True,
                    error="synthetic failure",
                ),
            )

        def export_state(self):
            return {"counter": self.counter}

        def restore_state(self, state):
            self.counter = state["counter"]

    processor = GrowthRateProcessor(
        run_id="failure",
        params={**_params(), "alignment_method": "none"},
        uv_struct_list=[_uv(), _uv()],
        kernel=SimpleNamespace(k_c_cell=[], k_o_cell=[]),
        latest_overlay_path=tmp_path / "latest.jpg",
    )
    processor.alignment_method = "fft"
    processor.aligner = FailingAligner()
    processor.aligner_initialized = True
    result = processor.process(image_path)

    assert not result.valid
    assert result.alignment is not None
    assert result.alignment.error == "synthetic failure"
    assert processor.aligner.counter == 0
    assert processor.algorithm_step == 0
    assert processor.uv_structs[0].dist_array == []


def test_legacy_processor_state_is_accepted_only_for_none(tmp_path: Path) -> None:
    source = GrowthRateProcessor(
        run_id="legacy",
        params=_params(),
        uv_struct_list=[_uv(), _uv()],
        kernel=SimpleNamespace(k_c_cell=[], k_o_cell=[]),
        latest_overlay_path=tmp_path / "latest.jpg",
    )
    legacy = source.export_state()
    legacy["schema_version"] = 1
    legacy.pop("alignment")

    restored = GrowthRateProcessor(
        run_id="legacy",
        params=_params(),
        uv_struct_list=[_uv(), _uv()],
        kernel=SimpleNamespace(k_c_cell=[], k_o_cell=[]),
        latest_overlay_path=tmp_path / "latest.jpg",
    )
    restored.restore_state(legacy)
    assert restored.alignment_method == "none"

    selected = GrowthRateProcessor(
        run_id="legacy",
        params={**_params(), "alignment_method": "fft"},
        uv_struct_list=[_uv(), _uv()],
        kernel=SimpleNamespace(k_c_cell=[], k_o_cell=[]),
        latest_overlay_path=tmp_path / "latest.jpg",
        initial_image_path=tmp_path / "baseline.png",
    )
    with pytest.raises(ValueError, match="Legacy processor state"):
        selected.restore_state(legacy)


@pytest.mark.parametrize("case", ["version2", "missing_alignment", "null_alignment", "wrong_method", "missing_sidecar", "tampered_sidecar"])
def test_unified_state_fails_closed_for_invalid_alignment(tmp_path: Path, case: str) -> None:
    def make():
        return GrowthRateProcessor(
            run_id="state-safety", params={**_params(), "alignment_method": "fft"},
            uv_struct_list=[_uv(), _uv()], kernel=SimpleNamespace(k_c_cell=[], k_o_cell=[]),
            latest_overlay_path=tmp_path / "latest.jpg", initial_image_path=tmp_path / "baseline.png",
        )
    source = make()
    mask = np.ones((32, 32), dtype=np.uint8) * 255
    source.aligner.initialize(AlignmentInput(mask, mask, mask))
    source.aligner_initialized = True
    state = source.export_state()
    if case == "version2":
        state["schema_version"] = 2
    elif case == "missing_alignment":
        state.pop("alignment")
    elif case == "null_alignment":
        state["alignment"] = None
    elif case == "wrong_method":
        state["alignment"]["method"] = "loftr"
    elif case == "missing_sidecar":
        (tmp_path / "alignment_state.npz").unlink()
    else:
        path = tmp_path / "alignment_state.npz"
        path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises((ValueError, OSError)):
        make().restore_state(state)
