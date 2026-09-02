from __future__ import annotations

import cv2
import numpy as np
import pytest

from crystallization_mpc.apps.gsensor.alignment import (
    ALIGNMENT_METHODS,
    AlignmentInput,
    alignment_capabilities,
    create_aligner,
    parse_alignment_method,
)


def _translated_pair() -> tuple[AlignmentInput, AlignmentInput]:
    mask = np.zeros((512, 512), dtype=np.uint8)
    cv2.rectangle(mask, (140, 150), (360, 380), 255, -1)
    gray = np.zeros_like(mask)
    cv2.circle(gray, (250, 260), 70, 220, -1)
    cv2.line(gray, (180, 200), (320, 330), 100, 5)
    shift = np.float32([[1, 0, 4], [0, 1, -3]])
    shifted_gray = cv2.warpAffine(gray, shift, gray.shape[::-1])
    shifted_mask = cv2.warpAffine(mask, shift, mask.shape[::-1], flags=cv2.INTER_NEAREST)
    return (
        AlignmentInput(gray, mask, mask),
        AlignmentInput(shifted_gray, shifted_mask, shifted_mask),
    )


def test_registry_exposes_all_methods_and_rejects_unknown() -> None:
    assert ALIGNMENT_METHODS == (
        "none",
        "centroid",
        "fft",
        "kalman",
        "loftr",
        "sift",
        "optical_flow",
        "ecc",
    )
    assert parse_alignment_method("fft").value == "fft"
    with pytest.raises(ValueError, match="Unsupported alignment method"):
        create_aligner("not-a-method")


def test_capabilities_do_not_import_optional_loftr_eagerly() -> None:
    capabilities = {item.method: item for item in alignment_capabilities()}
    assert set(capabilities) == set(ALIGNMENT_METHODS)
    assert all(capabilities[name].available for name in ALIGNMENT_METHODS if name != "loftr")
    if not capabilities["loftr"].available:
        assert capabilities["loftr"].reason


@pytest.mark.parametrize("method", ["none", "centroid", "fft", "kalman"])
def test_translation_methods_use_current_to_reference_convention(method: str) -> None:
    first, shifted = _translated_pair()
    aligner = create_aligner(method)
    assert aligner.initialize(first).diagnostics.success
    result = aligner.align(shifted)
    assert result.diagnostics.success
    if method == "none":
        assert np.allclose(result.transform, np.eye(2, 3), atol=0.01)
    else:
        assert result.transform[0, 2] == pytest.approx(-4, abs=1)
        assert result.transform[1, 2] == pytest.approx(3, abs=1)


def test_failure_does_not_advance_fft_reference_state() -> None:
    first, _ = _translated_pair()
    aligner = create_aligner("fft")
    aligner.initialize(first)
    before = aligner.export_state()["previous_mask"].copy()
    empty = np.zeros_like(first.image_gray)
    result = aligner.align(AlignmentInput(empty, empty, empty))
    assert not result.diagnostics.success
    assert np.array_equal(aligner.export_state()["previous_mask"], before)
