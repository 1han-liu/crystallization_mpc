"""Safe, atomic persistence for stateful frame aligners."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .registry import parse_alignment_method


ALIGNMENT_STATE_VERSION = 2


def save_alignment_state(
    path: str | Path,
    *,
    method: str,
    frame_sequence: int,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically save an aligner's arrays and scalar metadata to compressed NPZ."""

    selected = parse_alignment_method(method).value
    if state.get("method") != selected:
        raise ValueError("alignment state method does not match selected method")
    if frame_sequence < 0:
        raise ValueError("alignment frame_sequence must be non-negative")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, np.ndarray] = {}
    scalars: dict[str, Any] = {}
    for key, value in state.items():
        if not isinstance(key, str) or not key:
            raise ValueError("alignment state keys must be non-empty strings")
        if isinstance(value, np.ndarray):
            array = np.asarray(value)
            if array.dtype.hasobject or not np.all(np.isfinite(array)):
                raise ValueError(f"alignment state array {key!r} is unsafe")
            arrays[key] = array.copy()
        else:
            scalars[key] = _json_value(value, key)

    metadata = {
        "version": ALIGNMENT_STATE_VERSION,
        "method": selected,
        "frame_sequence": int(frame_sequence),
        "scalars": scalars,
        "array_keys": sorted(arrays),
    }
    payload = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    arrays["__metadata__"] = np.asarray(payload)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            np.savez_compressed(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "version": ALIGNMENT_STATE_VERSION,
        "method": selected,
        "frame_sequence": int(frame_sequence),
        "path": target.name,
        "sha256": _sha256(target),
    }


def load_alignment_state(
    path: str | Path,
    *,
    expected_method: str,
    expected_frame_sequence: int | None = None,
    expected_sha256: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Load and validate an NPZ sidecar without allowing pickle objects."""

    target = Path(path)
    selected = parse_alignment_method(expected_method).value
    if expected_sha256 is not None and _sha256(target) != expected_sha256:
        raise ValueError("alignment state checksum does not match")

    try:
        with np.load(target, allow_pickle=False) as archive:
            if "__metadata__" not in archive.files:
                raise ValueError("alignment state metadata is missing")
            raw_metadata = archive["__metadata__"]
            if raw_metadata.shape != ():
                raise ValueError("alignment state metadata has invalid shape")
            metadata = json.loads(str(raw_metadata.item()))
            _validate_metadata(metadata, selected, expected_frame_sequence)

            expected_arrays = set(metadata["array_keys"])
            actual_arrays = set(archive.files) - {"__metadata__"}
            if actual_arrays != expected_arrays:
                raise ValueError("alignment state array list does not match metadata")

            state = dict(metadata["scalars"])
            for key in sorted(expected_arrays):
                value = np.asarray(archive[key])
                if value.dtype.hasobject or not np.all(np.isfinite(value)):
                    raise ValueError(f"alignment state array {key!r} is unsafe")
                state[key] = value.copy()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid alignment state sidecar: {exc}") from exc

    if state.get("method") != selected:
        raise ValueError("alignment state payload method does not match metadata")
    return int(metadata["frame_sequence"]), state


def _validate_metadata(
    metadata: Any,
    selected: str,
    expected_frame_sequence: int | None,
) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("alignment state metadata must be an object")
    if metadata.get("version") != ALIGNMENT_STATE_VERSION:
        raise ValueError("unsupported alignment state version")
    if metadata.get("method") != selected:
        raise ValueError("alignment state method does not match configuration")
    frame_sequence = metadata.get("frame_sequence")
    if not isinstance(frame_sequence, int) or frame_sequence < 0:
        raise ValueError("alignment state frame_sequence is invalid")
    if expected_frame_sequence is not None and frame_sequence != expected_frame_sequence:
        raise ValueError("alignment state frame_sequence does not match processing state")
    if not isinstance(metadata.get("scalars"), dict):
        raise ValueError("alignment state scalar payload is invalid")
    array_keys = metadata.get("array_keys")
    if not isinstance(array_keys, list) or any(
        not isinstance(key, str) or not key or key == "__metadata__" for key in array_keys
    ):
        raise ValueError("alignment state array keys are invalid")
    if len(array_keys) != len(set(array_keys)):
        raise ValueError("alignment state array keys contain duplicates")


def _json_value(value: Any, key: str) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        return [_json_value(item, key) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not np.isfinite(value):
            raise ValueError(f"alignment state scalar {key!r} is non-finite")
        return value
    raise ValueError(f"alignment state scalar {key!r} is not JSON-safe")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "ALIGNMENT_STATE_VERSION",
    "load_alignment_state",
    "save_alignment_state",
]
