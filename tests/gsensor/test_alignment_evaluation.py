from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
from PIL import Image

from crystallization_mpc.apps.gsensor.alignment.evaluation import evaluate_sequence


def test_cached_sequence_evaluation_writes_json_and_csv(tmp_path: Path) -> None:
    input_path = tmp_path / "images"
    output_path = tmp_path / "output"
    cache_path = output_path / "segmentation_cache"
    input_path.mkdir()
    cache_path.mkdir(parents=True)
    base = np.zeros((512, 512), dtype=np.uint8)
    base[100:420, 100:420] = 180
    mask = np.zeros_like(base)
    mask[100:420, 100:420] = 1
    for index, dx in enumerate((0, 3, 6)):
        name = f"frame_{index:03d}.png"
        image = np.roll(base, dx, axis=1)
        Image.fromarray(image).save(input_path / name)
        shifted_mask = np.roll(mask, dx, axis=1)
        with (cache_path / f"{index:05d}.npz").open("wb") as stream:
            np.savez_compressed(
                stream,
                image_name=np.asarray(name),
                raw_mask=shifted_mask,
                measurement_mask=shifted_mask,
            )

    report = evaluate_sequence(
        input_path,
        output_path,
        methods=["none", "centroid"],
    )
    assert report["image_count"] == 3
    assert [item["method"] for item in report["summaries"]] == ["none", "centroid"]
    assert Path(report["json_path"]).is_file()
    with Path(report["csv_path"]).open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 6
    assert {row["method"] for row in rows} == {"none", "centroid"}
