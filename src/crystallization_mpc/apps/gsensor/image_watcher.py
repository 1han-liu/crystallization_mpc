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
    """Scan once, returning readable files not previously processed.

    Files are identified by their direct child filename only. This intentionally
    does not detect overwrites of the same filename in the minimal first version.
    """

    image_directory = Path(directory).expanduser().resolve(strict=False)
    if not image_directory.is_dir():
        raise FileNotFoundError(f"Experiment image directory not found: {image_directory}")

    processed = set(processed_files)
    probe = image_probe or verify_image_readable
    candidates: list[tuple[int, str, Path, float]] = []
    stat_failures: list[str] = []
    for path in image_directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if path.name in processed:
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            stat_failures.append(f"{path.name}: {exc}")
            continue
        candidates.append((stat.st_mtime_ns, path.name, path, stat.st_mtime))

    candidates.sort(key=lambda item: (item[0], item[1]))
    detections: list[DetectedImage] = []
    read_failures: list[str] = []
    for _mtime_ns, image_name, path, modified_seconds in candidates:
        try:
            probe(path)
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            read_failures.append(f"{image_name}: {exc}")
            continue
        detection = DetectedImage(
            image_name=image_name,
            file_modified_at=_utc_iso_from_timestamp(modified_seconds),
            detected_at=timestamp_factory(),
        )
        detections.append(detection)
        processed.add(image_name)

    errors = [*stat_failures, *read_failures]
    pending_count = len(stat_failures) + sum(
        1 for _mtime_ns, image_name, _path, _modified in candidates if image_name not in processed
    )
    return ImageScanResult(
        detections=tuple(detections),
        processed_files=frozenset(processed),
        pending_image_count=pending_count,
        scanned_at=timestamp_factory(),
        last_error="; ".join(errors) if errors else None,
    )


def verify_image_readable(path: Path) -> None:
    """Open and verify an image without retaining decoded pixel data."""

    with Image.open(path) as image:
        image.verify()


def _utc_iso_from_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "DetectedImage",
    "ImageScanResult",
    "scan_new_images",
    "verify_image_readable",
]
