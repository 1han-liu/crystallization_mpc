"""Public types shared by the GSensor frame-alignment implementations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import numpy as np


class AlignmentMethod(str, Enum):
    NONE = "none"
    CENTROID = "centroid"
    FFT = "fft"
    KALMAN = "kalman"
    LOFTR = "loftr"
    SIFT = "sift"
    OPTICAL_FLOW = "optical_flow"
    ECC = "ecc"


ALIGNMENT_METHODS = tuple(method.value for method in AlignmentMethod)


@dataclass(frozen=True)
class AlignmentInput:
    """One decoded frame and the two masks used by an alignment method."""

    image_gray: np.ndarray
    raw_mask: np.ndarray
    measurement_mask: np.ndarray


@dataclass(frozen=True)
class AlignmentDiagnostics:
    method: str
    success: bool
    fallback_used: bool = False
    error: str | None = None
    tx_px: float = 0.0
    ty_px: float = 0.0
    rotation_deg: float = 0.0
    runtime_ms: float = 0.0
    residual_before: float | None = None
    residual_after: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AlignmentResult:
    aligned_gray: np.ndarray
    aligned_measurement_mask: np.ndarray
    transform: np.ndarray
    diagnostics: AlignmentDiagnostics


@dataclass(frozen=True)
class AlignmentCapability:
    method: str
    available: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class FrameAligner(Protocol):
    method: AlignmentMethod

    def initialize(self, frame: AlignmentInput) -> AlignmentResult:
        ...

    def align(self, frame: AlignmentInput) -> AlignmentResult:
        ...

    def export_state(self) -> dict[str, Any]:
        ...

    def restore_state(self, state: dict[str, Any]) -> None:
        ...


def normalize_input(frame: AlignmentInput) -> AlignmentInput:
    gray = np.asarray(frame.image_gray)
    if gray.ndim != 2:
        raise ValueError("alignment image_gray must be a two-dimensional image")
    gray = np.clip(gray, 0, 255).astype(np.uint8, copy=True)

    masks: list[np.ndarray] = []
    for name, value in (
        ("raw_mask", frame.raw_mask),
        ("measurement_mask", frame.measurement_mask),
    ):
        mask = np.asarray(value)
        if mask.ndim != 2 or mask.shape != gray.shape:
            raise ValueError(f"alignment {name} must match image_gray shape")
        masks.append(np.where(mask > 0, 255, 0).astype(np.uint8))
    return AlignmentInput(gray, masks[0], masks[1])


def identity_transform() -> np.ndarray:
    return np.eye(2, 3, dtype=np.float32)


def validate_transform(transform: np.ndarray) -> np.ndarray:
    matrix = np.asarray(transform, dtype=np.float32)
    if matrix.shape != (2, 3):
        raise ValueError("alignment transform must have shape (2, 3)")
    if not np.all(np.isfinite(matrix)):
        raise ValueError("alignment transform must contain only finite values")
    return matrix.copy()


__all__ = [
    "ALIGNMENT_METHODS",
    "AlignmentCapability",
    "AlignmentDiagnostics",
    "AlignmentInput",
    "AlignmentMethod",
    "AlignmentResult",
    "FrameAligner",
    "identity_transform",
    "normalize_input",
    "validate_transform",
]
