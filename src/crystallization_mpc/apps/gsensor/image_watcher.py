"""Minimal polling-based discovery of new experiment images."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from crystallization_mpc.apps.gsensor.initialization import IMAGE_EXTENSIONS
from crystallization_mpc.messaging.schema import utc_ts

ImageProbe = Callable[[Path], None]
TimestampFactory = Callable[[], str]


@dataclass(frozen=True)
class DetectedImage:
    image_name: str
    identity_key: str
    modified_time_ns: int
    file_size: int
    file_modified_at: str
    detected_at: str


@dataclass(frozen=True)
class ImageScanResult:
    detections: tuple[DetectedImage, ...]
    processed_files: frozenset[str]
    pending_image_count: int
    scanned_at: str
    last_error: str | None = None


def scan_new_images(
    directory: str | Path,
    processed_files: Iterable[str],
    *,
    image_probe: ImageProbe | None = None,
    timestamp_factory: TimestampFactory = utc_ts,
) -> ImageScanResult:
    """Scan once, returning readable image revisions not previously processed.

    A revision is identified by filename, nanosecond modification time, and file
    size. A camera may therefore overwrite a fixed filename without losing the
    new frame. Bare filenames remain accepted as legacy identifiers so an old
    runtime state can be upgraded without replaying its existing files.
    """

    image_directory = Path(directory).expanduser().resolve(strict=False)
    if not image_directory.is_dir():
        raise FileNotFoundError(f"Experiment image directory not found: {image_directory}")

    processed = set(processed_files)
    probe = image_probe or verify_image_readable
    candidates: list[tuple[int, str, Path, float, int, str]] = []
    stat_failures: list[str] = []
    for path in image_directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            stat_failures.append(f"{path.name}: {exc}")
            continue
        identity_key = image_identity_key(
            path.name,
            modified_time_ns=stat.st_mtime_ns,
            file_size=stat.st_size,
        )
        if path.name in processed or identity_key in processed:
            continue
        candidates.append(
            (
                stat.st_mtime_ns,
                path.name,
                path,
                stat.st_mtime,
                stat.st_size,
                identity_key,
            )
        )

    candidates.sort(key=lambda item: (item[0], item[1]))
    detections: list[DetectedImage] = []
    read_failures: list[str] = []
    for (
        modified_time_ns,
        image_name,
        path,
        modified_seconds,
        file_size,
        identity_key,
    ) in candidates:
        try:
            probe(path)
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            read_failures.append(f"{image_name}: {exc}")
            continue
        detection = DetectedImage(
            image_name=image_name,
            identity_key=identity_key,
            modified_time_ns=modified_time_ns,
            file_size=file_size,
            file_modified_at=_utc_iso_from_timestamp(modified_seconds),
            detected_at=timestamp_factory(),
        )
        detections.append(detection)
        processed.add(identity_key)

    errors = [*stat_failures, *read_failures]
    pending_count = len(stat_failures) + sum(
        1
        for _mtime_ns, _name, _path, _modified, _size, identity_key in candidates
        if identity_key not in processed
    )
    return ImageScanResult(
        detections=tuple(detections),
        processed_files=frozenset(processed),
        pending_image_count=pending_count,
        scanned_at=timestamp_factory(),
        last_error="; ".join(errors) if errors else None,
    )


def verify_image_readable(path: Path) -> None:
    """Fully decode image pixels before admitting a camera frame.

    ``Image.verify()`` validates every PNG chunk, including optional metadata.
    Some camera/export tools emit readable pixel data with a bad checksum in an
    ancillary chunk (for example ``mtAc``).  The measurement pipeline does not
    consume that metadata, so pixel decoding is the relevant integrity check.
    ``load()`` still rejects truncated or corrupt image data while accepting
    frames whose non-pixel metadata is malformed.
    """

    with Image.open(path) as image:
        image.load()


def image_identity_key(
    image_name: str,
    *,
    modified_time_ns: int,
    file_size: int,
) -> str:
    """Return the stable, JSON-safe identity of one image file revision."""

    return f"v1:{int(modified_time_ns)}:{int(file_size)}:{image_name}"


def _utc_iso_from_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "DetectedImage",
    "ImageScanResult",
    "image_identity_key",
    "scan_new_images",
    "verify_image_readable",
]
