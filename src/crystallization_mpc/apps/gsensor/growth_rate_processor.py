"""Stateful single-frame growth-rate processing shared by live and offline flows."""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np

from crystallization_mpc.apps.gsensor.alignment import (
    ALIGNMENT_STATE_VERSION,
    AlignmentDiagnostics,
    AlignmentInput,
    create_aligner,
    load_alignment_state,
    parse_alignment_method,
    save_alignment_state,
)
from crystallization_mpc.apps.gsensor.detection.find_edge_points_yolov import (
    YoloV8SegRunner,
    edge_points_from_measurement_mask,
    segment_crystal_yolov,
)
from crystallization_mpc.apps.gsensor.detection.initial_uv_struct import (
    initialize_uv_struct,
)
from crystallization_mpc.apps.gsensor.detection.params import build_params_G
from crystallization_mpc.apps.gsensor.detection.update_EKF_G import update_EKF_G
from crystallization_mpc.apps.gsensor.detection.update_figure import update_figure
from crystallization_mpc.apps.gsensor.detection.update_uv_struct import update_uv_struct
from crystallization_mpc.apps.gsensor.detection.update_line import imread
from crystallization_mpc.messaging.schema import utc_ts
from crystallization_mpc.apps.gsensor.experiments import (
    GSENSOR_PROCESSING_SCHEMA_VERSION,
    validate_processing_schema_version,
)

LATEST_OVERLAY_FILENAME = "gsensor_detection_latest.jpg"
FINAL_OVERLAY_FILENAME = "gsensor_detection_final.jpg"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EdgeMeasurement:
    detected: bool
    distance_px: float
    distance_m: float
    distance_KF_m: float
    G: float
    G_KF: float


@dataclass(frozen=True)
class GrowthRateFrameResult:
    run_id: str
    frame_seq: int
    image_name: str
    image_path: str
    captured_at: str | None
    detected_at: str | None
    processed_at: str
    processing_duration_ms: float
    dt_s: float
    unit: str
    valid: bool
    error: str | None
    overlay_path: str | None
    u: EdgeMeasurement | None
    v: EdgeMeasurement | None
    alignment: AlignmentDiagnostics | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GrowthRateProcessor:
    """Own u/v tracking and Kalman state while processing one experiment."""

    def __init__(
        self,
        *,
        run_id: str,
        params: Mapping[str, Any],
        uv_struct_list: Sequence[Any],
        kernel: Any,
        latest_overlay_path: str | Path | None = None,
        final_overlay_path: str | Path | None = None,
        overlay_directory: str | Path | None = None,
        debug_directory: str | Path | None = None,
        initial_image_path: str | Path | None = None,
        alignment_method: str | None = None,
        segmentation_runner: YoloV8SegRunner | None = None,
        alignment_state_path: str | Path | None = None,
    ) -> None:
        if len(uv_struct_list) != 2:
            raise ValueError("uv_struct_list must contain u and v structures.")
        if latest_overlay_path is not None and overlay_directory is not None:
            raise ValueError(
                "Use latest_overlay_path for live output or overlay_directory for per-frame output."
            )

        self.run_id = str(run_id)
        self.dt_s = float(_required(params, "dt_G"))
        self.resolution = float(_required(params, "resolution"))
        if self.dt_s <= 0:
            raise ValueError("dt_G must be greater than zero.")
        if self.resolution <= 0:
            raise ValueError("resolution must be greater than zero.")
        self.params_G = build_params_G(params)
        self.kernel = kernel
        self.latest_overlay_path = (
            Path(latest_overlay_path) if latest_overlay_path is not None else None
        )
        self.final_overlay_path = (
            Path(final_overlay_path) if final_overlay_path is not None else None
        )
        self.overlay_directory = (
            Path(overlay_directory) if overlay_directory is not None else None
        )
        self.debug_directory = (
            Path(debug_directory) if debug_directory is not None else None
        )
        self.initial_image_path = (
            Path(initial_image_path) if initial_image_path is not None else None
        )
        configured_method = (
            alignment_method
            if alignment_method is not None
            else str(params.get("alignment_method", "none"))
        )
        self.alignment_method = parse_alignment_method(configured_method).value
        if self.alignment_method != "none" and self.initial_image_path is None:
            raise ValueError(
                "initial_image_path is required when alignment_method is not 'none'."
            )
        self.aligner = create_aligner(self.alignment_method)
        self.aligner_initialized = False
        self.segmentation_runner = segmentation_runner
        if alignment_state_path is not None:
            self.alignment_state_path = Path(alignment_state_path)
        elif self.latest_overlay_path is not None:
            self.alignment_state_path = self.latest_overlay_path.parent / "alignment_state.npz"
        elif self.overlay_directory is not None:
            self.alignment_state_path = self.overlay_directory.parent / "alignment_state.npz"
        else:
            self.alignment_state_path = None
        self.frame_seq = 0
        self.algorithm_step = 0
        self.valid_frame_count = 0
        self.invalid_frame_count = 0
        self.last_result: GrowthRateFrameResult | None = None

        q2 = _required(params, "q2")
        r_diag = _required(params, "r_diag")
        self.uv_structs = [
            initialize_uv_struct(item, self.dt_s, self.resolution, q2, r_diag)
            for item in uv_struct_list
        ]

    def process(
        self,
        image_file: str | Path,
        *,
        captured_at: str | None = None,
        detected_at: str | None = None,
    ) -> GrowthRateFrameResult:
        """Process exactly one post-baseline image and retain state for the next one."""

        image_path = Path(image_file)
        self.frame_seq += 1
        frame_seq = self.frame_seq
        started = time.perf_counter()
        overlay_path: Path | None = None
        u_measurement: EdgeMeasurement | None = None
        v_measurement: EdgeMeasurement | None = None
        alignment_diagnostics: AlignmentDiagnostics | None = None
        alignment_checkpoint: dict[str, Any] | None = None

        try:
            original_image = imread(image_path)
            segmentation = segment_crystal_yolov(
                original_image,
                runner=self.segmentation_runner,
            )
            current_frame = AlignmentInput(
                image_gray=_to_gray(original_image),
                raw_mask=np.asarray(segmentation.raw_mask, dtype=np.uint8) * 255,
                measurement_mask=(
                    np.asarray(segmentation.measurement_mask, dtype=np.uint8) * 255
                ),
            )
            if (
                self.alignment_method != "none"
                and not np.any(current_frame.measurement_mask)
            ):
                alignment_diagnostics = AlignmentDiagnostics(
                    method=self.alignment_method,
                    success=False,
                    error="current segmentation mask is empty",
                )
                raise RuntimeError(alignment_diagnostics.error)
            if not self.aligner_initialized:
                baseline_frame = self._alignment_baseline(current_frame, image_path)
                if (
                    self.alignment_method != "none"
                    and not np.any(baseline_frame.measurement_mask)
                ):
                    alignment_diagnostics = AlignmentDiagnostics(
                        method=self.alignment_method,
                        success=False,
                        error="baseline segmentation mask is empty",
                    )
                    raise RuntimeError(alignment_diagnostics.error)
                initial_result = self.aligner.initialize(baseline_frame)
                if not initial_result.diagnostics.success:
                    alignment_diagnostics = initial_result.diagnostics
                    raise RuntimeError(
                        "alignment initialization failed: "
                        f"{initial_result.diagnostics.error or 'unknown error'}"
                    )
                self.aligner_initialized = True

            alignment_checkpoint = self.aligner.export_state()
            try:
                alignment_result = self.aligner.align(current_frame)
                alignment_diagnostics = alignment_result.diagnostics
                if not alignment_diagnostics.success:
                    raise RuntimeError(
                        "alignment failed: "
                        f"{alignment_diagnostics.error or 'unknown error'}"
                    )
            except Exception:
                self.aligner.restore_state(alignment_checkpoint)
                raise

            shared_edge_mask = edge_points_from_measurement_mask(
                alignment_result.aligned_measurement_mask,
                self.kernel,
            )
            algorithm_step = self.algorithm_step + 1
            working_structs = deepcopy(self.uv_structs)
            for index, edge_name in enumerate(("u", "v")):
                debug_label = f"frame{frame_seq:05d}_{edge_name}"
                working_structs[index], original_image = update_uv_struct(
                    working_structs[index],
                    image_path,
                    algorithm_step,
                    self.params_G,
                    self.kernel,
                    debug_dir=self.debug_directory,
                    debug_label=debug_label,
                    original_image=alignment_result.aligned_gray,
                    edge_mask=shared_edge_mask,
                )
                working_structs[index] = update_EKF_G(
                    working_structs[index],
                    self.dt_s,
                    self.resolution,
                    algorithm_step,
                )

            overlay_path = self._write_overlay(
                original_image,
                image_path,
                uv_structs=working_structs,
            )
            u_measurement = self._edge_measurement(working_structs[0], algorithm_step)
            v_measurement = self._edge_measurement(working_structs[1], algorithm_step)
            missing_edges = [
                name
                for name, measurement in (("u", u_measurement), ("v", v_measurement))
                if not measurement.detected
            ]
            valid = not missing_edges
            measurements = (u_measurement, v_measurement)
            finite = all(
                np.isfinite(value)
                for measurement in measurements
                for value in (
                    measurement.distance_px,
                    measurement.distance_m,
                    measurement.distance_KF_m,
                    measurement.G,
                    measurement.G_KF,
                )
            )
            if missing_edges:
                error = f"no valid {'/'.join(missing_edges)} Hough line detected"
            elif not finite:
                valid = False
                error = "growth-rate calculation produced a non-finite value"
                u_measurement = None
                v_measurement = None
            else:
                error = None
            if valid and finite:
                self.uv_structs = working_structs
                self.algorithm_step = algorithm_step
            elif alignment_checkpoint is not None:
                self.aligner.restore_state(alignment_checkpoint)
        except Exception as exc:
            if alignment_checkpoint is not None:
                self.aligner.restore_state(alignment_checkpoint)
            valid = False
            error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Growth-rate frame failed: run_id=%s frame_seq=%s image=%s",
                self.run_id,
                frame_seq,
                image_path,
            )

        if valid:
            self.valid_frame_count += 1
        else:
            self.invalid_frame_count += 1

        result = GrowthRateFrameResult(
            run_id=self.run_id,
            frame_seq=frame_seq,
            image_name=image_path.name,
            image_path=str(image_path),
            captured_at=captured_at,
            detected_at=detected_at,
            processed_at=utc_ts(),
            processing_duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            dt_s=self.dt_s,
            unit="m/s",
            valid=valid,
            error=error,
            overlay_path=str(overlay_path) if overlay_path is not None else None,
            u=u_measurement,
            v=v_measurement,
            alignment=alignment_diagnostics,
        )
        self.last_result = result
        return result

    def _alignment_baseline(
        self,
        current_frame: AlignmentInput,
        current_path: Path,
    ) -> AlignmentInput:
        if self.initial_image_path is None or self.initial_image_path == current_path:
            return current_frame
        baseline_image = imread(self.initial_image_path)
        segmentation = segment_crystal_yolov(
            baseline_image,
            runner=self.segmentation_runner,
        )
        return AlignmentInput(
            image_gray=_to_gray(baseline_image),
            raw_mask=np.asarray(segmentation.raw_mask, dtype=np.uint8) * 255,
            measurement_mask=(
                np.asarray(segmentation.measurement_mask, dtype=np.uint8) * 255
            ),
        )

    def finalize(self) -> Path | None:
        """Freeze the most recent live overlay when Central stops the experiment."""

        if self.latest_overlay_path is None or self.final_overlay_path is None:
            return None
        if not self.latest_overlay_path.is_file():
            return None

        self.final_overlay_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.final_overlay_path.with_name(
            f".{self.final_overlay_path.stem}.{uuid4().hex}.tmp"
            f"{self.final_overlay_path.suffix}"
        )
        try:
            shutil.copyfile(self.latest_overlay_path, temporary)
            os.replace(temporary, self.final_overlay_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return self.final_overlay_path

    def export_state(self) -> dict[str, Any]:
        """Export only the numerical state required to continue the next frame."""

        alignment = {
            "version": ALIGNMENT_STATE_VERSION,
            "method": self.alignment_method,
            "initialized": self.aligner_initialized,
            "sidecar": None,
        }
        if self.aligner_initialized:
            if self.alignment_state_path is None:
                raise ValueError("alignment_state_path is required to persist alignment state.")
            alignment["sidecar"] = save_alignment_state(
                self.alignment_state_path,
                method=self.alignment_method,
                frame_sequence=self.frame_seq,
                state=self.aligner.export_state(),
            )
        return {
            "schema_version": GSENSOR_PROCESSING_SCHEMA_VERSION,
            "run_id": self.run_id,
            "frame_seq": self.frame_seq,
            "algorithm_step": self.algorithm_step,
            "valid_frame_count": self.valid_frame_count,
            "invalid_frame_count": self.invalid_frame_count,
            "edges": [self._export_edge_state(item) for item in self.uv_structs],
            "alignment": alignment,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """Restore a state produced by :meth:`export_state`."""

        validate_processing_schema_version(state.get("schema_version"))
        if str(state.get("run_id") or "") != self.run_id:
            raise ValueError("Growth-rate processor state run_id does not match.")
        edges = state.get("edges")
        if not isinstance(edges, list) or len(edges) != 2:
            raise ValueError("Growth-rate processor state must contain u and v edges.")

        frame_seq = _non_negative_int(state.get("frame_seq"), "frame_seq")
        algorithm_step = _non_negative_int(
            state.get("algorithm_step"), "algorithm_step"
        )
        if algorithm_step > frame_seq:
            raise ValueError("algorithm_step cannot exceed frame_seq.")

        # Only old, unaligned v1 records may omit alignment metadata. Extended
        # v1 records restore alignment by field presence, not by another version.
        if "alignment" not in state:
            if self.alignment_method != "none":
                raise ValueError(
                    "Legacy processor state can only be restored with alignment_method='none'."
                )
            self.aligner_initialized = False
        else:
            alignment = state.get("alignment")
            if not isinstance(alignment, Mapping):
                raise ValueError("Growth-rate alignment state is missing.")
            if alignment.get("method") != self.alignment_method:
                raise ValueError("Growth-rate alignment method does not match configuration.")
            initialized = bool(alignment.get("initialized", False))
            sidecar = alignment.get("sidecar")
            if initialized:
                if self.alignment_state_path is None or not isinstance(sidecar, Mapping):
                    raise ValueError("Growth-rate alignment sidecar metadata is missing.")
                sidecar_name = str(sidecar.get("path") or "")
                if sidecar_name != self.alignment_state_path.name:
                    raise ValueError("Growth-rate alignment sidecar path does not match.")
                _sequence, aligner_state = load_alignment_state(
                    self.alignment_state_path,
                    expected_method=self.alignment_method,
                    expected_frame_sequence=frame_seq,
                    expected_sha256=str(sidecar.get("sha256") or ""),
                )
                self.aligner.restore_state(aligner_state)
            elif sidecar is not None:
                raise ValueError("Uninitialized alignment state must not contain a sidecar.")
            self.aligner_initialized = initialized

        for uv_struct, edge_state in zip(self.uv_structs, edges):
            self._restore_edge_state(uv_struct, edge_state, algorithm_step)
        self.frame_seq = frame_seq
        self.algorithm_step = algorithm_step
        self.valid_frame_count = _non_negative_int(
            state.get("valid_frame_count"), "valid_frame_count"
        )
        self.invalid_frame_count = _non_negative_int(
            state.get("invalid_frame_count"), "invalid_frame_count"
        )

    @staticmethod
    def _export_edge_state(uv_struct: Any) -> dict[str, Any]:
        line = uv_struct.line
        ekf = uv_struct.EKF_G
        return {
            "line": {
                "point1": _float_list(line.point1),
                "point2": _float_list(line.point2),
                "theta": float(line.theta),
                "rho": float(line.rho),
                "detection_valid": bool(
                    getattr(line, "detection_valid", True)
                ),
            },
            "baseline_dist": float(getattr(uv_struct, "baseline_dist", 0.0)),
            "dist_array": _nullable_float_list(uv_struct.dist_array),
            "distance_array": _nullable_float_list(uv_struct.distance_array),
            "distance_KF_array": _nullable_float_list(
                uv_struct.distance_KF_array
            ),
            "G_array": _nullable_float_list(uv_struct.G_array),
            "G_KF_array": _nullable_float_list(uv_struct.G_KF_array),
            "x_G_array": np.asarray(uv_struct.x_G_array, dtype=float).tolist(),
            "kalman": {
                "x": np.asarray(ekf.x, dtype=float).tolist(),
                "P": np.asarray(ekf.P, dtype=float).tolist(),
            },
        }

    @staticmethod
    def _restore_edge_state(
        uv_struct: Any,
        state: Any,
        algorithm_step: int,
    ) -> None:
        if not isinstance(state, Mapping):
            raise ValueError("Growth-rate edge state must be an object.")
        line = state.get("line")
        kalman = state.get("kalman")
        if not isinstance(line, Mapping) or not isinstance(kalman, Mapping):
            raise ValueError("Growth-rate edge line/Kalman state is missing.")
        uv_struct.line = SimpleNamespace(
            point1=np.asarray(line.get("point1"), dtype=float),
            point2=np.asarray(line.get("point2"), dtype=float),
            theta=float(line.get("theta")),
            rho=float(line.get("rho")),
            detection_valid=bool(line.get("detection_valid", True)),
        )
        uv_struct.baseline_dist = float(state.get("baseline_dist", 0.0))
        for name in (
            "dist_array",
            "distance_array",
            "distance_KF_array",
            "G_array",
            "G_KF_array",
        ):
            values = state.get(name)
            if not isinstance(values, list) or len(values) != algorithm_step:
                raise ValueError(
                    f"Growth-rate edge {name} length does not match algorithm_step."
                )
            setattr(
                uv_struct,
                name,
                [None if value is None else float(value) for value in values],
            )
        x_history = np.asarray(state.get("x_G_array"), dtype=float)
        expected_shape = (3, algorithm_step)
        if algorithm_step == 0 and x_history.size == 0:
            x_history = np.zeros(expected_shape, dtype=float)
        if x_history.shape != expected_shape:
            raise ValueError("Growth-rate x_G_array shape is invalid.")
        uv_struct.x_G_array = x_history

        x = np.asarray(kalman.get("x"), dtype=float)
        covariance = np.asarray(kalman.get("P"), dtype=float)
        if x.shape not in {(3,), (3, 1)} or covariance.shape != (3, 3):
            raise ValueError("Growth-rate Kalman state shape is invalid.")
        if not np.all(np.isfinite(x)) or not np.all(np.isfinite(covariance)):
            raise ValueError("Growth-rate Kalman state must be finite.")
        uv_struct.EKF_G.x = x.reshape(3, 1)
        uv_struct.EKF_G.P = covariance

    def _write_overlay(
        self,
        original_image: Any,
        image_file: Path,
        *,
        uv_structs: Sequence[Any],
    ) -> Path:
        if self.overlay_directory is not None:
            return update_figure(
                uv_structs[0],
                uv_structs[1],
                original_image,
                image_file,
                output_dir=self.overlay_directory,
            )
        if self.latest_overlay_path is None:
            raise ValueError("An overlay output path is required.")
        return update_figure(
            uv_structs[0],
            uv_structs[1],
            original_image,
            image_file,
            output_path=self.latest_overlay_path,
        )

    def _edge_measurement(self, uv_struct: Any, frame_seq: int) -> EdgeMeasurement:
        return EdgeMeasurement(
            detected=bool(getattr(uv_struct.line, "detection_valid", True)),
            distance_px=_value(uv_struct.dist_array, frame_seq),
            distance_m=_value(uv_struct.distance_array, frame_seq),
            distance_KF_m=_value(uv_struct.distance_KF_array, frame_seq),
            G=_value(uv_struct.G_array, frame_seq),
            G_KF=_value(uv_struct.G_KF_array, frame_seq),
        )


def _value(values: Any, frame_seq: int) -> float:
    array = np.asarray(values, dtype=object).reshape(-1)
    index = max(int(frame_seq) - 1, 0)
    if array.size <= index or array[index] is None:
        raise ValueError(f"Missing frame value at index {frame_seq}.")
    return float(np.asarray(array[index]).reshape(-1)[0])


def _required(params: Mapping[str, Any], key: str) -> Any:
    if key not in params:
        raise KeyError(f"Missing DSCGR parameter: {key}")
    return params[key]


def _float_list(value: Any) -> list[float]:
    return [float(item) for item in np.asarray(value, dtype=float).reshape(-1)]


def _nullable_float_list(value: Any) -> list[float | None]:
    return [
        None if item is None else float(np.asarray(item).reshape(-1)[0])
        for item in np.asarray(value, dtype=object).reshape(-1)
    ]


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _to_gray(image: Any) -> np.ndarray:
    array = np.asarray(image)
    if array.ndim == 2:
        return np.clip(array, 0, 255).astype(np.uint8)
    if array.ndim == 3 and array.shape[2] >= 3:
        # PIL-backed ``imread`` returns RGB data.
        return np.clip(
            0.299 * array[:, :, 0]
            + 0.587 * array[:, :, 1]
            + 0.114 * array[:, :, 2],
            0,
            255,
        ).astype(np.uint8)
    raise ValueError(f"Unsupported alignment image shape: {array.shape}")


__all__ = [
    "FINAL_OVERLAY_FILENAME",
    "LATEST_OVERLAY_FILENAME",
    "EdgeMeasurement",
    "GrowthRateFrameResult",
    "GrowthRateProcessor",
]
