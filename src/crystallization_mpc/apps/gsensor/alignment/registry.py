"""Alignment registry and stateful adapters around the pinned algorithms."""

from __future__ import annotations

import importlib
import time
from typing import Any

import cv2
import numpy as np

from . import centroid, ecc, fft, kalman, optical_flow, sift
from .types import (
    ALIGNMENT_METHODS,
    AlignmentCapability,
    AlignmentDiagnostics,
    AlignmentInput,
    AlignmentMethod,
    AlignmentResult,
    FrameAligner,
    identity_transform,
    normalize_input,
    validate_transform,
)


def _residual(reference: np.ndarray | None, current: np.ndarray) -> float | None:
    if reference is None or reference.shape != current.shape:
        return None
    return float(np.mean(cv2.absdiff(reference, current)))


def _warp_gray(gray: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    height, width = gray.shape
    return cv2.warpAffine(
        gray,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def _identity_result(method: AlignmentMethod, frame: AlignmentInput) -> AlignmentResult:
    normalized = normalize_input(frame)
    return AlignmentResult(
        aligned_gray=normalized.image_gray.copy(),
        aligned_measurement_mask=normalized.measurement_mask.copy(),
        transform=identity_transform(),
        diagnostics=AlignmentDiagnostics(
            method=method.value,
            success=True,
            details={"initial_frame": True},
        ),
    )


def _result(
    method: AlignmentMethod,
    reference_gray: np.ndarray | None,
    current_gray: np.ndarray,
    aligned_gray: np.ndarray,
    aligned_mask: np.ndarray,
    matrix: np.ndarray,
    started: float,
    *,
    success: bool,
    error: str | None = None,
    fallback_used: bool = False,
    details: dict[str, Any] | None = None,
) -> AlignmentResult:
    transform = validate_transform(matrix)
    return AlignmentResult(
        aligned_gray=np.asarray(aligned_gray, dtype=np.uint8).copy(),
        aligned_measurement_mask=np.where(aligned_mask > 0, 255, 0).astype(np.uint8),
        transform=transform,
        diagnostics=AlignmentDiagnostics(
            method=method.value,
            success=bool(success),
            fallback_used=bool(fallback_used),
            error=error,
            tx_px=float(transform[0, 2]),
            ty_px=float(transform[1, 2]),
            rotation_deg=float(
                np.degrees(np.arctan2(transform[1, 0], transform[0, 0]))
            ),
            runtime_ms=float((time.perf_counter() - started) * 1000.0),
            residual_before=_residual(reference_gray, current_gray),
            residual_after=_residual(reference_gray, aligned_gray),
            details=dict(details or {}),
        ),
    )


class NoneAligner:
    method = AlignmentMethod.NONE

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        return _identity_result(self.method, frame)

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        return _identity_result(self.method, frame)

    def export_state(self) -> dict[str, Any]:
        return {"method": self.method.value}

    def restore_state(self, state: dict[str, Any]) -> None:
        _require_method(state, self.method)


class CentroidAligner:
    method = AlignmentMethod.CENTROID

    def __init__(self) -> None:
        self.previous_centroid: tuple[float, float] | None = None
        self.cumulative_dx = 0.0
        self.cumulative_dy = 0.0
        self.reference_gray: np.ndarray | None = None

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        _, self.previous_centroid, self.cumulative_dx, self.cumulative_dy = (
            centroid.align_one_frame_centroid(
                normalized.measurement_mask, None, 0.0, 0.0
            )
        )
        self.reference_gray = normalized.image_gray.copy()
        return _identity_result(self.method, normalized)

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        started = time.perf_counter()
        current_centroid = centroid.get_centroid(
            centroid.get_stable_mask(normalized.measurement_mask)
        )
        aligned, next_centroid, dx, dy = centroid.align_one_frame_centroid(
            normalized.measurement_mask,
            self.previous_centroid,
            self.cumulative_dx,
            self.cumulative_dy,
        )
        success = current_centroid is not None and self.previous_centroid is not None
        matrix = np.array(
            [[1.0, 0.0, round(dx)], [0.0, 1.0, round(dy)]], dtype=np.float32
        )
        warped = _warp_gray(normalized.image_gray, matrix)
        if success:
            self.previous_centroid = next_centroid
            self.cumulative_dx = float(dx)
            self.cumulative_dy = float(dy)
        return _result(
            self.method,
            self.reference_gray,
            normalized.image_gray,
            warped,
            aligned,
            matrix,
            started,
            success=success,
            error=None if success else "stable centroid is unavailable",
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "previous_centroid": self.previous_centroid,
            "cumulative_dx": self.cumulative_dx,
            "cumulative_dy": self.cumulative_dy,
            "reference_gray": self.reference_gray,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        _require_method(state, self.method)
        value = state.get("previous_centroid")
        if value is None:
            self.previous_centroid = None
        else:
            centroid_value = np.asarray(value, dtype=float)
            if centroid_value.shape != (2,) or not np.all(np.isfinite(centroid_value)):
                raise ValueError("centroid alignment state has an invalid centroid")
            self.previous_centroid = tuple(map(float, centroid_value))
        self.cumulative_dx = float(state.get("cumulative_dx", 0.0))
        self.cumulative_dy = float(state.get("cumulative_dy", 0.0))
        self.reference_gray = _optional_u8_image(state.get("reference_gray"), "reference_gray")


class FftAligner:
    method = AlignmentMethod.FFT

    def __init__(self) -> None:
        self.previous_mask: np.ndarray | None = None
        self.reference_gray: np.ndarray | None = None

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        self.previous_mask = normalized.measurement_mask.copy()
        self.reference_gray = normalized.image_gray.copy()
        return _identity_result(self.method, normalized)

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        started = time.perf_counter()
        if self.previous_mask is None or not np.any(normalized.measurement_mask):
            return _result(
                self.method,
                self.reference_gray,
                normalized.image_gray,
                normalized.image_gray,
                normalized.measurement_mask,
                identity_transform(),
                started,
                success=False,
                error="FFT reference or current mask is empty",
            )
        aligned, dx, dy, left_pixels = fft.align_one_frame(
            self.previous_mask, normalized.measurement_mask
        )
        matrix = np.array(
            [[1.0, 0.0, round(dx)], [0.0, 1.0, round(dy)]], dtype=np.float32
        )
        warped = _warp_gray(normalized.image_gray, matrix)
        self.previous_mask = aligned.copy()
        return _result(
            self.method,
            self.reference_gray,
            normalized.image_gray,
            warped,
            aligned,
            matrix,
            started,
            success=True,
            details={"uncovered_pixels": int(left_pixels)},
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "previous_mask": self.previous_mask,
            "reference_gray": self.reference_gray,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        _require_method(state, self.method)
        self.previous_mask = _optional_u8_image(state.get("previous_mask"), "previous_mask")
        self.reference_gray = _optional_u8_image(state.get("reference_gray"), "reference_gray")
        _require_same_shape(self.previous_mask, self.reference_gray)


class KalmanAligner:
    method = AlignmentMethod.KALMAN

    def __init__(self) -> None:
        self.filter = kalman.init_kalman_filter()
        self.initialized = False
        self.reference_gray: np.ndarray | None = None

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        measurement = kalman.get_centroid(
            kalman.get_stable_mask(normalized.measurement_mask)
        )
        if measurement is not None:
            self.filter.statePost = np.array(
                [[float(measurement[0, 0])], [float(measurement[1, 0])], [0.0], [0.0]],
                dtype=np.float32,
            )
            self.filter.errorCovPost = np.eye(4, dtype=np.float32) * 0.1
            self.initialized = True
        self.reference_gray = normalized.image_gray.copy()
        result = _identity_result(self.method, normalized)
        if measurement is None:
            return AlignmentResult(
                result.aligned_gray,
                result.aligned_measurement_mask,
                result.transform,
                AlignmentDiagnostics(
                    method=self.method.value,
                    success=False,
                    error="stable centroid is unavailable",
                    details={"initial_frame": True},
                ),
            )
        return result

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        started = time.perf_counter()
        measurement = kalman.get_centroid(
            kalman.get_stable_mask(normalized.measurement_mask)
        )
        if measurement is None or not self.initialized:
            return _result(
                self.method,
                self.reference_gray,
                normalized.image_gray,
                normalized.image_gray,
                normalized.measurement_mask,
                identity_transform(),
                started,
                success=False,
                error="Kalman centroid measurement is unavailable",
            )
        self.filter.predict()
        estimate = self.filter.correct(measurement)
        dx = float(estimate[0, 0] - measurement[0, 0])
        dy = float(estimate[1, 0] - measurement[1, 0])
        matrix = np.array(
            [[1.0, 0.0, round(dx)], [0.0, 1.0, round(dy)]], dtype=np.float32
        )
        aligned = cv2.warpAffine(
            normalized.measurement_mask,
            matrix,
            normalized.image_gray.shape[::-1],
            flags=cv2.INTER_NEAREST,
            borderValue=0,
        )
        warped = _warp_gray(normalized.image_gray, matrix)
        return _result(
            self.method,
            self.reference_gray,
            normalized.image_gray,
            warped,
            aligned,
            matrix,
            started,
            success=True,
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "initialized": self.initialized,
            "reference_gray": self.reference_gray,
            "statePost": self.filter.statePost.copy(),
            "errorCovPost": self.filter.errorCovPost.copy(),
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        _require_method(state, self.method)
        self.initialized = bool(state.get("initialized", False))
        self.reference_gray = _optional_u8_image(state.get("reference_gray"), "reference_gray")
        state_post = np.asarray(state["statePost"], dtype=np.float32)
        covariance = np.asarray(state["errorCovPost"], dtype=np.float32)
        if state_post.shape not in {(4,), (4, 1)} or covariance.shape != (4, 4):
            raise ValueError("Kalman alignment state has invalid matrix shapes")
        if not np.all(np.isfinite(state_post)) or not np.all(np.isfinite(covariance)):
            raise ValueError("Kalman alignment state contains non-finite values")
        self.filter.statePost = state_post.reshape(4, 1)
        self.filter.errorCovPost = covariance


class SiftAligner:
    method = AlignmentMethod.SIFT

    def __init__(self) -> None:
        self.detector, self.matcher = sift.init_sift()
        self.previous_gray: np.ndarray | None = None
        self.previous_seed: np.ndarray | None = None
        self.last_valid_transform = identity_transform()

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        self.previous_gray = normalized.image_gray.copy()
        self.previous_seed = sift.build_sift_seed_mask(
            normalized.measurement_mask, normalized.raw_mask
        )
        self.last_valid_transform = identity_transform()
        return _identity_result(self.method, normalized)

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        if self.previous_gray is None or self.previous_seed is None:
            raise RuntimeError("SIFT aligner has not been initialized")
        current_seed = sift.build_sift_seed_mask(
            normalized.measurement_mask, normalized.raw_mask
        )
        aligned_mask, aligned_seed, warped, matrix, raw = sift.align_one_frame_sift(
            self.previous_gray,
            normalized.image_gray,
            self.previous_seed,
            current_seed,
            normalized.measurement_mask,
            self.detector,
            self.matcher,
            fallback_matrix=self.last_valid_transform,
        )
        success = bool(raw["success"])
        if success:
            self.previous_gray = warped.copy()
            self.previous_seed = aligned_seed.copy()
            self.last_valid_transform = validate_transform(matrix)
        details = {
            key: value
            for key, value in raw.items()
            if key
            not in {
                "success",
                "fallback_reason",
                "tx",
                "ty",
                "rotation_deg",
                "runtime_s",
                "residual_before",
                "residual_after",
            }
        }
        return AlignmentResult(
            aligned_gray=warped,
            aligned_measurement_mask=aligned_mask,
            transform=validate_transform(matrix),
            diagnostics=AlignmentDiagnostics(
                method=self.method.value,
                success=success,
                fallback_used=not success,
                error=None if success else str(raw["fallback_reason"]),
                tx_px=float(raw["tx"]),
                ty_px=float(raw["ty"]),
                rotation_deg=float(raw["rotation_deg"]),
                runtime_ms=float(raw["runtime_s"]) * 1000.0,
                residual_before=float(raw["residual_before"]),
                residual_after=float(raw["residual_after"]),
                details=details,
            ),
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "previous_gray": self.previous_gray,
            "previous_seed": self.previous_seed,
            "last_valid_transform": self.last_valid_transform,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        _require_method(state, self.method)
        self.previous_gray = _optional_u8_image(state.get("previous_gray"), "previous_gray")
        self.previous_seed = _optional_u8_image(state.get("previous_seed"), "previous_seed")
        _require_same_shape(self.previous_gray, self.previous_seed)
        self.last_valid_transform = validate_transform(state["last_valid_transform"])


class LoftrAligner:
    method = AlignmentMethod.LOFTR

    def __init__(self) -> None:
        self.algorithm = importlib.import_module(
            "crystallization_mpc.apps.gsensor.alignment.loftr"
        )
        self.matcher, self.device = self.algorithm.init_loftr()
        self.previous_gray: np.ndarray | None = None
        self.previous_seed: np.ndarray | None = None
        self.last_valid_transform = identity_transform()

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        self.previous_gray = normalized.image_gray.copy()
        self.previous_seed = self.algorithm.build_loftr_seed_mask(
            normalized.measurement_mask, normalized.raw_mask
        )
        self.last_valid_transform = identity_transform()
        return _identity_result(self.method, normalized)

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        if self.previous_gray is None or self.previous_seed is None:
            raise RuntimeError("LoFTR aligner has not been initialized")
        current_seed = self.algorithm.build_loftr_seed_mask(
            normalized.measurement_mask, normalized.raw_mask
        )
        aligned_mask, aligned_seed, warped, matrix, raw = (
            self.algorithm.align_one_frame_loftr(
                self.previous_gray,
                normalized.image_gray,
                self.previous_seed,
                current_seed,
                normalized.measurement_mask,
                self.matcher,
                self.device,
                fallback_matrix=self.last_valid_transform,
            )
        )
        success = bool(raw["success"])
        if success:
            self.previous_gray = warped.copy()
            self.previous_seed = aligned_seed.copy()
            self.last_valid_transform = validate_transform(matrix)
        details = {
            key: value
            for key, value in raw.items()
            if key
            not in {
                "success",
                "fallback_reason",
                "tx",
                "ty",
                "rotation_deg",
                "runtime_s",
                "residual_before",
                "residual_after",
            }
        }
        return AlignmentResult(
            aligned_gray=warped,
            aligned_measurement_mask=aligned_mask,
            transform=validate_transform(matrix),
            diagnostics=AlignmentDiagnostics(
                method=self.method.value,
                success=success,
                fallback_used=not success,
                error=None if success else str(raw["fallback_reason"]),
                tx_px=float(raw["tx"]),
                ty_px=float(raw["ty"]),
                rotation_deg=float(raw["rotation_deg"]),
                runtime_ms=float(raw["runtime_s"]) * 1000.0,
                residual_before=float(raw["residual_before"]),
                residual_after=float(raw["residual_after"]),
                details=details,
            ),
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "previous_gray": self.previous_gray,
            "previous_seed": self.previous_seed,
            "last_valid_transform": self.last_valid_transform,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        _require_method(state, self.method)
        self.previous_gray = _optional_u8_image(state.get("previous_gray"), "previous_gray")
        self.previous_seed = _optional_u8_image(state.get("previous_seed"), "previous_seed")
        _require_same_shape(self.previous_gray, self.previous_seed)
        self.last_valid_transform = validate_transform(state["last_valid_transform"])


class OpticalFlowAligner:
    method = AlignmentMethod.OPTICAL_FLOW

    def __init__(self) -> None:
        self.previous_enhanced: np.ndarray | None = None
        self.previous_mask: np.ndarray | None = None
        self.reference_gray: np.ndarray | None = None

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        bgr = cv2.cvtColor(normalized.image_gray, cv2.COLOR_GRAY2BGR)
        self.previous_enhanced = optical_flow.enhance_contrast(bgr)
        self.previous_mask = normalized.measurement_mask.copy()
        self.reference_gray = normalized.image_gray.copy()
        return _identity_result(self.method, normalized)

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        started = time.perf_counter()
        if self.previous_enhanced is None or self.previous_mask is None:
            raise RuntimeError("optical-flow aligner has not been initialized")
        aligned, enhanced, dx, dy, points, source_matrix = (
            optical_flow.align_one_frame_optical_flow(
                self.previous_enhanced,
                normalized.image_gray,
                self.previous_mask,
                normalized.measurement_mask,
            )
        )
        matrix = cv2.invertAffineTransform(source_matrix).astype(np.float32)
        warped = _warp_gray(normalized.image_gray, matrix)
        success = int(points) > 0
        if success:
            self.previous_enhanced = _warp_gray(enhanced, matrix)
            self.previous_mask = aligned.copy()
        return _result(
            self.method,
            self.reference_gray,
            normalized.image_gray,
            warped,
            aligned,
            matrix,
            started,
            success=success,
            error=None if success else "no valid optical-flow tracks",
            details={"tracked_points": int(points), "source_dx": dx, "source_dy": dy},
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "previous_enhanced": self.previous_enhanced,
            "previous_mask": self.previous_mask,
            "reference_gray": self.reference_gray,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        _require_method(state, self.method)
        self.previous_enhanced = _optional_u8_image(
            state.get("previous_enhanced"), "previous_enhanced"
        )
        self.previous_mask = _optional_u8_image(state.get("previous_mask"), "previous_mask")
        self.reference_gray = _optional_u8_image(state.get("reference_gray"), "reference_gray")
        _require_same_shape(self.previous_enhanced, self.previous_mask, self.reference_gray)


class EccAligner:
    method = AlignmentMethod.ECC

    def __init__(self) -> None:
        self.previous_enhanced: np.ndarray | None = None
        self.previous_mask: np.ndarray | None = None
        self.previous_warp = identity_transform()
        self.warp_buffer: list[np.ndarray] = []
        self.frame_index = 0
        self.reference_gray: np.ndarray | None = None

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        self.previous_enhanced = ecc.enhance_for_ecc(normalized.image_gray)
        self.previous_mask = normalized.measurement_mask.copy()
        self.previous_warp = identity_transform()
        self.warp_buffer = []
        self.frame_index = 0
        self.reference_gray = normalized.image_gray.copy()
        return _identity_result(self.method, normalized)

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        normalized = normalize_input(frame)
        started = time.perf_counter()
        if self.previous_enhanced is None:
            raise RuntimeError("ECC aligner has not been initialized")
        self.frame_index += 1
        aligned, enhanced, source_matrix, buffer, raw = ecc.align_one_frame_ecc(
            self.previous_enhanced,
            normalized.image_gray,
            normalized.measurement_mask,
            self.previous_warp,
            self.warp_buffer,
            self.frame_index,
            self.previous_mask,
        )
        matrix = cv2.invertAffineTransform(source_matrix).astype(np.float32)
        warped = _warp_gray(normalized.image_gray, matrix)
        success = bool(raw["success"]) and bool(np.any(normalized.measurement_mask))
        if success:
            self.previous_enhanced = _warp_gray(enhanced, matrix)
            self.previous_mask = aligned.copy()
            self.previous_warp = validate_transform(raw["next_warp"])
            self.warp_buffer = [np.asarray(item, dtype=np.float32).copy() for item in buffer]
        return _result(
            self.method,
            self.reference_gray,
            normalized.image_gray,
            warped,
            aligned,
            matrix,
            started,
            success=success,
            error=(
                None
                if success
                else str(raw.get("fallback_reason") or "ECC measurement mask is empty")
            ),
            fallback_used=not success,
            details={
                key: value
                for key, value in raw.items()
                if key not in {"success", "fallback_reason", "next_warp"}
            },
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "previous_enhanced": self.previous_enhanced,
            "previous_mask": self.previous_mask,
            "previous_warp": self.previous_warp,
            "warp_buffer": np.asarray(self.warp_buffer, dtype=np.float32),
            "frame_index": self.frame_index,
            "reference_gray": self.reference_gray,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        _require_method(state, self.method)
        self.previous_enhanced = _optional_u8_image(
            state.get("previous_enhanced"), "previous_enhanced"
        )
        self.previous_mask = _optional_u8_image(state.get("previous_mask"), "previous_mask")
        self.previous_warp = validate_transform(state["previous_warp"])
        buffer = np.asarray(state.get("warp_buffer", []), dtype=np.float32)
        if buffer.size == 0:
            buffer = np.empty((0, 2, 3), dtype=np.float32)
        if buffer.ndim != 3 or buffer.shape[1:] != (2, 3):
            raise ValueError("ECC alignment warp_buffer has invalid shape")
        self.warp_buffer = [validate_transform(item) for item in buffer]
        self.frame_index = int(state.get("frame_index", 0))
        self.reference_gray = _optional_u8_image(state.get("reference_gray"), "reference_gray")
        _require_same_shape(self.previous_enhanced, self.previous_mask, self.reference_gray)


ALIGNER_TYPES: dict[AlignmentMethod, type[FrameAligner]] = {
    AlignmentMethod.NONE: NoneAligner,
    AlignmentMethod.CENTROID: CentroidAligner,
    AlignmentMethod.FFT: FftAligner,
    AlignmentMethod.KALMAN: KalmanAligner,
    AlignmentMethod.LOFTR: LoftrAligner,
    AlignmentMethod.SIFT: SiftAligner,
    AlignmentMethod.OPTICAL_FLOW: OpticalFlowAligner,
    AlignmentMethod.ECC: EccAligner,
}


def parse_alignment_method(value: str | AlignmentMethod) -> AlignmentMethod:
    try:
        return value if isinstance(value, AlignmentMethod) else AlignmentMethod(str(value))
    except ValueError as exc:
        raise ValueError(
            f"Unsupported alignment method {value!r}; expected one of "
            f"{', '.join(ALIGNMENT_METHODS)}"
        ) from exc


def create_aligner(method: str | AlignmentMethod) -> FrameAligner:
    selected = parse_alignment_method(method)
    return ALIGNER_TYPES[selected]()


def alignment_capabilities() -> list[AlignmentCapability]:
    result: list[AlignmentCapability] = []
    for method in AlignmentMethod:
        try:
            if method is AlignmentMethod.LOFTR:
                algorithm = importlib.import_module(
                    "crystallization_mpc.apps.gsensor.alignment.loftr"
                )
                weight_path = algorithm.loftr_weights_path()
                if not weight_path.is_file():
                    raise RuntimeError(f"LoFTR outdoor weights are missing: {weight_path}")
            elif method is AlignmentMethod.SIFT and not hasattr(cv2, "SIFT_create"):
                raise RuntimeError("OpenCV was built without SIFT support")
            result.append(AlignmentCapability(method.value, True))
        except Exception as exc:
            result.append(
                AlignmentCapability(
                    method.value,
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return result


def _require_method(state: dict[str, Any], method: AlignmentMethod) -> None:
    if state.get("method") != method.value:
        raise ValueError(
            f"alignment state method {state.get('method')!r} does not match {method.value!r}"
        )


def _optional_array(value: Any) -> np.ndarray | None:
    if value is None:
        return None
    array = np.asarray(value)
    if not np.all(np.isfinite(array)):
        raise ValueError("alignment state contains non-finite array data")
    return array.copy()


def _optional_u8_image(value: Any, name: str) -> np.ndarray | None:
    array = _optional_array(value)
    if array is None:
        return None
    if array.ndim != 2 or array.dtype != np.uint8:
        raise ValueError(f"alignment state {name} must be a two-dimensional uint8 image")
    return array


def _require_same_shape(*values: np.ndarray | None) -> None:
    shapes = {value.shape for value in values if value is not None}
    if len(shapes) > 1:
        raise ValueError("alignment state image shapes do not match")


__all__ = [
    "alignment_capabilities",
    "create_aligner",
    "parse_alignment_method",
]
