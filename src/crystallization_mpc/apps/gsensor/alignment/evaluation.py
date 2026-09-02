"""Offline sequence evaluator for the GSensor alignment implementations."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterable

import numpy as np

from crystallization_mpc.apps.gsensor.detection.find_edge_points_yolov import (
    get_default_runner,
    segment_crystal_yolov,
)
from crystallization_mpc.apps.gsensor.detection.update_line import imread

from .registry import create_aligner, parse_alignment_method
from .types import ALIGNMENT_METHODS, AlignmentInput


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
CSV_FIELDS = (
    "method",
    "frame_index",
    "image_name",
    "success",
    "fallback_used",
    "error",
    "tx_px",
    "ty_px",
    "rotation_deg",
    "runtime_ms",
    "residual_before",
    "residual_after",
    "rss_mib",
    "gpu_peak_mib",
)


def evaluate_sequence(
    input_directory: str | Path,
    output_directory: str | Path,
    *,
    methods: Iterable[str] = ALIGNMENT_METHODS,
    limit: int | None = None,
    dt_s: float = 15.0,
) -> dict:
    input_path = Path(input_directory).resolve()
    output_path = Path(output_directory).resolve()
    images = sorted(
        (path for path in input_path.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda path: _natural_key(path.name),
    )
    if limit is not None:
        images = images[:limit]
    if len(images) < 2:
        raise ValueError("alignment evaluation requires at least two images")
    selected = [parse_alignment_method(method).value for method in methods]
    if len(selected) != len(set(selected)):
        raise ValueError("alignment evaluation methods must be unique")
    if dt_s <= 0:
        raise ValueError("dt_s must be positive")
    # Keep the memory-heavy model last so its allocator does not pollute the
    # traditional-method memory measurements.
    selected = [method for method in selected if method != "loftr"] + [
        method for method in selected if method == "loftr"
    ]

    output_path.mkdir(parents=True, exist_ok=True)
    cache_path = output_path / "segmentation_cache"
    cache_path.mkdir(parents=True, exist_ok=True)
    _prepare_segmentation_cache(images, cache_path)

    all_records: list[dict] = []
    summaries: list[dict] = []
    for method in selected:
        records = _evaluate_method(method, images, cache_path)
        all_records.extend(records)
        summaries.append(_summarize(method, records, len(images), dt_s))

    report = {
        "schema_version": 1,
        "input_directory": str(input_path),
        "output_directory": str(output_path),
        "image_count": len(images),
        "methods": selected,
        "coordinate_convention": "current_frame_to_initial_reference",
        "scope": "alignment-only; Hough and growth-rate acceptance require calibration",
        "dt_s": dt_s,
        "summaries": summaries,
        "records": all_records,
    }
    json_path = output_path / "alignment_evaluation.json"
    csv_path = output_path / "alignment_evaluation.csv"
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(all_records)
    report["json_path"] = str(json_path)
    report["csv_path"] = str(csv_path)
    return report


def _prepare_segmentation_cache(images: list[Path], cache_path: Path) -> None:
    runner = None
    for index, image_path in enumerate(images):
        destination = cache_path / f"{index:05d}.npz"
        if destination.is_file():
            with np.load(destination, allow_pickle=False) as archive:
                if str(archive["image_name"].item()) == image_path.name:
                    continue
        runner = runner or get_default_runner()
        image = imread(image_path)
        segmentation = segment_crystal_yolov(image, runner=runner)
        with destination.open("wb") as stream:
            np.savez_compressed(
                stream,
                image_name=np.asarray(image_path.name),
                raw_mask=np.asarray(segmentation.raw_mask, dtype=np.uint8),
                measurement_mask=np.asarray(segmentation.measurement_mask, dtype=np.uint8),
            )


def _evaluate_method(method: str, images: list[Path], cache_path: Path) -> list[dict]:
    if "torch" in sys.modules and sys.modules["torch"].cuda.is_available():
        sys.modules["torch"].cuda.reset_peak_memory_stats()
    aligner = create_aligner(method)
    first = _load_frame(images[0], cache_path / "00000.npz")
    initial = aligner.initialize(first)
    records = [_record(method, 0, images[0].name, initial.diagnostics)]
    for index, image_path in enumerate(images[1:], start=1):
        frame = _load_frame(image_path, cache_path / f"{index:05d}.npz")
        checkpoint = aligner.export_state()
        started = time.perf_counter()
        try:
            result = aligner.align(frame)
            diagnostics = result.diagnostics
            if not diagnostics.success:
                aligner.restore_state(checkpoint)
        except Exception as exc:
            aligner.restore_state(checkpoint)
            from .types import AlignmentDiagnostics

            diagnostics = AlignmentDiagnostics(
                method=method,
                success=False,
                fallback_used=True,
                error=f"{type(exc).__name__}: {exc}",
                runtime_ms=(time.perf_counter() - started) * 1000.0,
            )
        records.append(_record(method, index, image_path.name, diagnostics))
    return records


def _load_frame(image_path: Path, cache_file: Path) -> AlignmentInput:
    image = np.asarray(imread(image_path))
    if image.ndim == 3:
        gray = np.clip(
            0.299 * image[:, :, 0] + 0.587 * image[:, :, 1] + 0.114 * image[:, :, 2],
            0,
            255,
        ).astype(np.uint8)
    else:
        gray = np.asarray(image, dtype=np.uint8)
    with np.load(cache_file, allow_pickle=False) as archive:
        raw = np.asarray(archive["raw_mask"], dtype=np.uint8) * 255
        measurement = np.asarray(archive["measurement_mask"], dtype=np.uint8) * 255
    return AlignmentInput(gray, raw, measurement)


def _record(method: str, index: int, image_name: str, diagnostics) -> dict:
    payload = diagnostics.to_dict()
    return {
        "method": method,
        "frame_index": index,
        "image_name": image_name,
        "success": bool(payload["success"]),
        "fallback_used": bool(payload["fallback_used"]),
        "error": payload["error"] or "",
        "tx_px": _finite_or_none(payload["tx_px"]),
        "ty_px": _finite_or_none(payload["ty_px"]),
        "rotation_deg": _finite_or_none(payload["rotation_deg"]),
        "runtime_ms": _finite_or_none(payload["runtime_ms"]),
        "residual_before": _finite_or_none(payload["residual_before"]),
        "residual_after": _finite_or_none(payload["residual_after"]),
        "rss_mib": _current_rss_mib(),
        "gpu_peak_mib": _gpu_peak_mib(),
    }


def _summarize(method: str, records: list[dict], image_count: int, dt_s: float) -> dict:
    evaluated = records[1:]
    successes = [item for item in evaluated if item["success"]]
    failures = [item for item in evaluated if not item["success"]]
    runtimes = [item["runtime_ms"] for item in evaluated if item["runtime_ms"] is not None]
    before = [item["residual_before"] for item in successes if item["residual_before"] is not None]
    after = [item["residual_after"] for item in successes if item["residual_after"] is not None]
    translations = np.asarray(
        [[item["tx_px"], item["ty_px"]] for item in successes], dtype=float
    )
    rotations = np.asarray([item["rotation_deg"] for item in successes], dtype=float)
    translation_jump = (
        np.linalg.norm(np.diff(translations, axis=0), axis=1)
        if len(translations) > 1
        else np.asarray([], dtype=float)
    )
    rotation_jump = np.abs(np.diff(rotations)) if len(rotations) > 1 else np.asarray([])
    return {
        "method": method,
        "image_count": image_count,
        "evaluated_frames": len(evaluated),
        "successful_frames": len(successes),
        "failed_frames": len(failures),
        "success_rate": len(successes) / len(evaluated) if evaluated else 0.0,
        "fallback_rate": sum(item["fallback_used"] for item in evaluated) / len(evaluated)
        if evaluated
        else 0.0,
        "mean_runtime_ms": float(np.mean(runtimes)) if runtimes else None,
        "p95_runtime_ms": float(np.percentile(runtimes, 95)) if runtimes else None,
        "within_dt_rate": sum(runtime < dt_s * 1000.0 for runtime in runtimes)
        / len(runtimes)
        if runtimes
        else None,
        "mean_residual_before": float(np.mean(before)) if before else None,
        "mean_residual_after": float(np.mean(after)) if after else None,
        "peak_rss_mib": max(item["rss_mib"] for item in records),
        "peak_gpu_mib": max(item["gpu_peak_mib"] for item in records),
        "mean_translation_jump_px": (
            float(np.mean(translation_jump)) if translation_jump.size else None
        ),
        "mean_rotation_jump_deg": (
            float(np.mean(rotation_jump)) if rotation_jump.size else None
        ),
        "hough_uv_acceptance": "NOT_RUN: calibration geometry unavailable",
        "growth_rate_acceptance": "NOT_RUN: calibration geometry unavailable",
    }


def _finite_or_none(value):
    if value is None:
        return None
    number = float(value)
    return number if np.isfinite(number) else None


def _natural_key(value: str):
    import re

    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def _current_rss_mib() -> float:
    with Path("/proc/self/statm").open("r", encoding="ascii") as stream:
        pages = int(stream.read().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0)


def _gpu_peak_mib() -> float:
    torch = sys.modules.get("torch")
    if torch is None or not torch.cuda.is_available():
        return 0.0
    return float(torch.cuda.max_memory_allocated()) / (1024.0 * 1024.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--methods", nargs="+", default=list(ALIGNMENT_METHODS))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dt-s", type=float, default=15.0)
    arguments = parser.parse_args()
    report = evaluate_sequence(
        arguments.input,
        arguments.output,
        methods=arguments.methods,
        limit=arguments.limit,
        dt_s=arguments.dt_s,
    )
    print(json.dumps(report["summaries"], indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    main()
