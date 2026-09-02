"""Stateful image-alignment methods available to the GSensor service."""

from .registry import alignment_capabilities, create_aligner, parse_alignment_method
from .state import ALIGNMENT_STATE_VERSION, load_alignment_state, save_alignment_state
from .types import (
    ALIGNMENT_METHODS,
    AlignmentCapability,
    AlignmentDiagnostics,
    AlignmentInput,
    AlignmentMethod,
    AlignmentResult,
    FrameAligner,
)

__all__ = [
    "ALIGNMENT_METHODS",
    "ALIGNMENT_STATE_VERSION",
    "AlignmentCapability",
    "AlignmentDiagnostics",
    "AlignmentInput",
    "AlignmentMethod",
    "AlignmentResult",
    "FrameAligner",
    "alignment_capabilities",
    "create_aligner",
    "load_alignment_state",
    "parse_alignment_method",
    "save_alignment_state",
]
