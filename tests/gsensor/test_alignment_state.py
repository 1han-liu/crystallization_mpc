from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from crystallization_mpc.apps.gsensor.alignment import (
    ALIGNMENT_METHODS,
    AlignmentInput,
    create_aligner,
    load_alignment_state,
    save_alignment_state,
)


def test_state_round_trip_and_checksum(tmp_path: Path) -> None:
    target = tmp_path / "alignment.npz"
    state = {
        "method": "centroid",
        "previous_centroid": (12.5, 7.25),
        "cumulative_dx": -2.0,
        "cumulative_dy": 3.0,
        "reference_gray": np.arange(20, dtype=np.uint8).reshape(4, 5),
    }
    descriptor = save_alignment_state(
        target, method="centroid", frame_sequence=19, state=state
    )
    sequence, restored = load_alignment_state(
        target,
        expected_method="centroid",
        expected_frame_sequence=19,
        expected_sha256=descriptor["sha256"],
    )
    assert sequence == 19
    assert restored["previous_centroid"] == [12.5, 7.25]
    assert np.array_equal(restored["reference_gray"], state["reference_gray"])


def test_state_rejects_method_and_sequence_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "alignment.npz"
    descriptor = save_alignment_state(
        target,
        method="none",
        frame_sequence=2,
        state={"method": "none"},
    )
    with pytest.raises(ValueError, match="method"):
        load_alignment_state(target, expected_method="fft")
    with pytest.raises(ValueError, match="frame_sequence"):
        load_alignment_state(
            target,
            expected_method="none",
            expected_frame_sequence=3,
            expected_sha256=descriptor["sha256"],
        )


def test_state_rejects_tampering_and_unsafe_values(tmp_path: Path) -> None:
    target = tmp_path / "alignment.npz"
    descriptor = save_alignment_state(
        target,
        method="none",
        frame_sequence=0,
        state={"method": "none"},
    )
    target.write_bytes(target.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        load_alignment_state(
            target,
            expected_method="none",
            expected_sha256=descriptor["sha256"],
        )
    with pytest.raises(ValueError, match="unsafe"):
        save_alignment_state(
            tmp_path / "unsafe.npz",
            method="none",
            frame_sequence=0,
            state={"method": "none", "bad": np.array([object()])},
        )


@pytest.mark.parametrize("method", ALIGNMENT_METHODS)
def test_every_aligner_initial_state_can_be_persisted_and_restored(
    method: str,
    tmp_path: Path,
) -> None:
    if method == "loftr":
        pytest.importorskip("torch")
        pytest.importorskip("kornia.feature")
    mask = np.zeros((512, 512), dtype=np.uint8)
    mask[100:420, 90:430] = 255
    frame = AlignmentInput(mask.copy(), mask.copy(), mask.copy())
    source = create_aligner(method)
    source.initialize(frame)
    path = tmp_path / f"{method}.npz"
    descriptor = save_alignment_state(
        path,
        method=method,
        frame_sequence=0,
        state=source.export_state(),
    )
    _sequence, state = load_alignment_state(
        path,
        expected_method=method,
        expected_frame_sequence=0,
        expected_sha256=descriptor["sha256"],
    )
    restored = create_aligner(method)
    restored.restore_state(state)
    assert restored.export_state()["method"] == method


def test_aligner_restore_rejects_wrong_image_dtype() -> None:
    aligner = create_aligner("fft")
    with pytest.raises(ValueError, match="uint8"):
        aligner.restore_state(
            {
                "method": "fft",
                "previous_mask": np.ones((4, 4), dtype=np.float32),
                "reference_gray": np.ones((4, 4), dtype=np.uint8),
            }
        )
