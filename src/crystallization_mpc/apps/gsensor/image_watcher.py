"""Minimal polling-based discovery of new experiment images."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from crystallization_mpc.apps.gsensor.initialization import IMAGE_EXTENSIONS
from crystallization_mpc.messaging.schema import utc_ts

ImageProbe = Callable[[Path], None]
TimestampFactory = Callable[[], str]
_TRAILING_SEQUENCE = re.compile(r"(\d+)$")


@dataclass(frozen=True)
class ImageObservation:
    """One filesystem revision observed while it becomes stable."""

    image_name: str
    identity_key: str
    sequence_number: int | None
    modified_time_ns: int
    file_size: int
    file_modified_at: str
    stable_scan_count: int
    ready: bool = False
    ready_at: str | None = None


@dataclass(frozen=True)
class DetectedImage:
    image_name: str
    identity_key: str
    sequence_number: int | None
    modified_time_ns: int
    file_size: int
    file_modified_at: str
    detected_at: str


@dataclass(frozen=True)
class ImageScanResult:
    detections: tuple[DetectedImage, ...]
    processed_files: frozenset[str]
    file_identities: frozenset[str]
    observations: tuple[ImageObservation, ...]
    latest_discovered: ImageObservation | None
    latest_ready: DetectedImage | None
    pending_image_count: int
    scanned_at: str
    last_error: str | None = None


def scan_new_images(
    directory: str | Path,
    processed_files: Iterable[str],
    *,
    observations: Mapping[str, ImageObservation] | None = None,
    minimum_stable_scans: int = 2,
    image_probe: ImageProbe | None = None,
    timestamp_factory: TimestampFactory = utc_ts,
) -> ImageScanResult:
    """Scan once, returning stable readable revisions not previously processed.

    A revision is identified by filename, nanosecond modification time, and file
    size. A camera may therefore overwrite a fixed filename without losing the
    new frame. Bare filenames remain accepted as legacy identifiers so an old
    runtime state can be upgraded without replaying its existing files.

    Readiness belongs to an observation, not to the processed set. A readable
    revision remains ready and is returned on every scan until the caller adds
    its identity to ``processed_files``. This lets initialization keep watching
    newer frames without losing already-ready frames before measurement starts.
    """

    if minimum_stable_scans not in {1, 2}:
        raise ValueError("minimum_stable_scans must be 1 or 2.")

    image_directory = Path(directory).expanduser().resolve(strict=False)
    if not image_directory.is_dir():
        raise FileNotFoundError(f"Experiment image directory not found: {image_directory}")

    processed = set(processed_files)
    probe = image_probe or verify_image_readable
    previous_observations = dict(observations or {})
    current_observations: dict[str, ImageObservation] = {}
    current_file_identities: set[str] = set()
    candidates: list[tuple[tuple[int, int, int, str], Path, ImageObservation]] = []
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
        current_file_identities.add(identity_key)
        sequence_number = parse_image_sequence(path.name)
        previous = previous_observations.get(path.name)
        same_revision = previous is not None and previous.identity_key == identity_key
        stable_scan_count = (
            min(2, previous.stable_scan_count + 1) if same_revision else 1
        )
        observation = ImageObservation(
            image_name=path.name,
            identity_key=identity_key,
            sequence_number=sequence_number,
            modified_time_ns=stat.st_mtime_ns,
            file_size=stat.st_size,
            file_modified_at=_utc_iso_from_timestamp(stat.st_mtime),
            stable_scan_count=stable_scan_count,
            ready=bool(previous.ready) if same_revision else False,
            ready_at=previous.ready_at if same_revision else None,
        )
        if path.name in processed or identity_key in processed:
            continue
        current_observations[path.name] = observation
        candidates.append((_image_order_key(observation), path, observation))

    candidates.sort(key=lambda item: item[0])
    detections: list[DetectedImage] = []
    read_failures: list[str] = []
    for _order_key, path, observation in candidates:
        ready_observation = observation
        if not observation.ready:
            if observation.stable_scan_count < minimum_stable_scans:
                continue
            try:
                probe(path)
            except (OSError, UnidentifiedImageError, ValueError) as exc:
                read_failures.append(f"{observation.image_name}: {exc}")
                continue
            try:
                final_stat = path.stat()
            except OSError as exc:
                stat_failures.append(f"{observation.image_name}: {exc}")
                current_file_identities.discard(observation.identity_key)
                current_observations.pop(observation.image_name, None)
                continue
            final_identity_key = image_identity_key(
                observation.image_name,
                modified_time_ns=final_stat.st_mtime_ns,
                file_size=final_stat.st_size,
            )
            if final_identity_key != observation.identity_key:
                current_file_identities.discard(observation.identity_key)
                current_file_identities.add(final_identity_key)
                if (
                    observation.image_name in processed
                    or final_identity_key in processed
                ):
                    current_observations.pop(observation.image_name, None)
                else:
                    current_observations[observation.image_name] = ImageObservation(
                        image_name=observation.image_name,
                        identity_key=final_identity_key,
                        sequence_number=observation.sequence_number,
                        modified_time_ns=final_stat.st_mtime_ns,
                        file_size=final_stat.st_size,
                        file_modified_at=_utc_iso_from_timestamp(final_stat.st_mtime),
                        stable_scan_count=1,
                    )
                continue
            ready_observation = ImageObservation(
                image_name=observation.image_name,
                identity_key=observation.identity_key,
                sequence_number=observation.sequence_number,
                modified_time_ns=observation.modified_time_ns,
                file_size=observation.file_size,
                file_modified_at=observation.file_modified_at,
                stable_scan_count=min(2, observation.stable_scan_count),
                ready=True,
                ready_at=timestamp_factory(),
            )
            current_observations[observation.image_name] = ready_observation
        detection = DetectedImage(
            image_name=ready_observation.image_name,
            identity_key=ready_observation.identity_key,
            sequence_number=ready_observation.sequence_number,
            modified_time_ns=ready_observation.modified_time_ns,
            file_size=ready_observation.file_size,
            file_modified_at=ready_observation.file_modified_at,
            detected_at=ready_observation.ready_at or timestamp_factory(),
        )
        detections.append(detection)

    errors = [*stat_failures, *read_failures]
    ordered_observations = tuple(
        sorted(current_observations.values(), key=_image_order_key)
    )
    pending_count = len(stat_failures) + sum(
        1 for observation in ordered_observations if not observation.ready
    )
    return ImageScanResult(
        detections=tuple(detections),
        processed_files=frozenset(processed),
        file_identities=frozenset(current_file_identities),
        observations=ordered_observations,
        latest_discovered=(
            max(ordered_observations, key=_image_order_key)
            if ordered_observations
            else None
        ),
        latest_ready=(max(detections, key=_image_order_key) if detections else None),
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


def parse_image_sequence(image_name: str) -> int | None:
    """Extract the trailing numeric camera sequence from an image filename."""

    match = _TRAILING_SEQUENCE.search(Path(image_name).stem)
    return int(match.group(1)) if match else None


def _image_order_key(
    image: ImageObservation | DetectedImage,
) -> tuple[int, int, int, str]:
    """Order camera frames by source sequence, with an mtime legacy fallback."""

    if image.sequence_number is not None:
        return (
            1,
            image.sequence_number,
            image.modified_time_ns,
            image.image_name.casefold(),
        )
    return (0, image.modified_time_ns, 0, image.image_name.casefold())


def _utc_iso_from_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "DetectedImage",
    "ImageObservation",
    "ImageScanResult",
    "image_identity_key",
    "parse_image_sequence",
    "scan_new_images",
    "verify_image_readable",
]
