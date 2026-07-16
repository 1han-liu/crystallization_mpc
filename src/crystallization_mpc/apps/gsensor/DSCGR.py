"""Minimal offline DSCGR test script."""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from crystallization_mpc.apps.gsensor.detection.initial_uv_struct import initialize_uv_struct
from crystallization_mpc.apps.gsensor.detection.params import build_params_G
from crystallization_mpc.apps.gsensor.detection.update_EKF_G import update_EKF_G
from crystallization_mpc.apps.gsensor.detection.update_figure import update_figure
from crystallization_mpc.apps.gsensor.detection.update_uv_struct import update_uv_struct
from crystallization_mpc.apps.gsensor.utils.find_certain_image_file_by_date import (
    find_certain_image_file_by_date,
)
from crystallization_mpc.apps.gsensor.utils.find_image_files_by_date import (
    find_image_files_by_date,
)

logger = logging.getLogger(__name__)

CSV_FIELDS = (
    "ii",
    "ptr",
    "edge",
    "image_name",
    "image_path",
    "overlay_path",
    "distance",
    "distance_KF",
    "G",
    "G_KF",
)


def DSCGR(
    folder_G: str | Path,
    params: Mapping[str, Any],
    uv_struct_list: Sequence[Any],
    kernel: Any,
    *,
    output_dir: str | Path = "imgs",
    ptr_0: int = 1,
    print_fn: Callable[[str], None] | None = print,
) -> dict[str, Any]:
    """Run the offline DSCGR image sequence test.

    ``ptr_0`` is used as the initialization image. The offline test then
    processes every following image frame: 2, 3, 4, ... when ``ptr_0=1``.
    """

    folder = Path(folder_G)
    output_path = Path(output_dir)
    overlay_dir = output_path / "overlays"
    output_path.mkdir(parents=True, exist_ok=True)
    overlay_dir.mkdir(parents=True, exist_ok=True)

    image_files = find_image_files_by_date(folder)
    length_image_files = len(image_files)
    initial_image_file = find_certain_image_file_by_date(folder, ptr_0)
    if initial_image_file is None:
        raise FileNotFoundError(f"No initial DSCGR image found for pointer {ptr_0}.")
    logger.warning(
        "DSCGR start: folder=%s image_count=%s ptr_0=%s initial_image=%s output_dir=%s",
        folder,
        length_image_files,
        ptr_0,
        initial_image_file.name,
        output_path,
    )

    if len(uv_struct_list) != 2:
        raise ValueError("uv_struct_list must contain u and v structures.")

    dt_G = _required(params, "dt_G")
    resolution = _required(params, "resolution")
    q2 = _required(params, "q2")
    r_diag = _required(params, "r_diag")
    params_G = build_params_G(params)

    uv_structs = list(uv_struct_list)
    for kk in range(2):
        uv_structs[kk] = initialize_uv_struct(
            uv_structs[kk],
            dt_G,
            resolution,
            q2,
            r_diag,
        )

    records: list[dict[str, Any]] = []
    processed_ptrs: list[int] = []
    uv_names = ("u", "v")
    ptr = int(ptr_0)
    ii = 1

    while True:
        ptr += 1
        if ptr > length_image_files:
            break
        next_image_file = find_certain_image_file_by_date(folder, ptr)
        if next_image_file is None:
            raise FileNotFoundError(f"No image found for DSCGR pointer {ptr}.")
        logger.warning("DSCGR frame start: ptr=%s ii=%s image=%s", ptr, ii, next_image_file)

        frame_records = []
        log_parts = [f"{ptr}: "]
        I_orig = None
        for kk, uv_name in enumerate(uv_names):
            uv_structs[kk], I_orig = update_uv_struct(
                uv_structs[kk],
                next_image_file,
                ii,
                params_G,
                kernel,
            )
            uv_structs[kk] = update_EKF_G(uv_structs[kk], dt_G, resolution, ii)
            record = _record(uv_structs[kk], uv_name, next_image_file, ptr, ii)
            frame_records.append(record)
            log_parts.append(
                f"{uv_name} | measured: {_format(record['G'])}, "
                f"KF: {_format(record['G_KF'])}; "
            )

        overlay_path = update_figure(
            uv_structs[0],
            uv_structs[1],
            I_orig,
            next_image_file,
            output_dir=overlay_dir,
        )
        for record in frame_records:
            record["overlay_path"] = str(overlay_path)
            records.append(record)

        processed_ptrs.append(ptr)
        logger.warning("DSCGR frame done: ptr=%s ii=%s overlay=%s", ptr, ii, overlay_path)
        if print_fn is not None:
            print_fn("".join(log_parts))
        ii += 1

    payload = {
        "mode": "offline_dscgr",
        "image_folder": str(folder),
        "output_dir": str(output_path),
        "overlay_dir": str(overlay_dir),
        "ptr_0": int(ptr_0),
        "initial_image": str(initial_image_file),
        "length_image_files": length_image_files,
        "processed_ptrs": processed_ptrs,
        "records": records,
    }
    json_path = output_path / "DSCGR_data.json"
    csv_path = output_path / "DSCGR_data.csv"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_csv(csv_path, records)
    payload["json_path"] = str(json_path)
    payload["csv_path"] = str(csv_path)
    logger.warning(
        "DSCGR done: processed_ptrs=%s records=%s json=%s csv=%s",
        processed_ptrs,
        len(records),
        json_path,
        csv_path,
    )
    return payload


def _record(uv_struct: Any, edge: str, image_file: Path, ptr: int, ii: int) -> dict[str, Any]:
    return {
        "ii": int(ii),
        "ptr": int(ptr),
        "edge": edge,
        "image_name": image_file.name,
        "image_path": str(image_file),
        "overlay_path": "",
        "distance": _value(getattr(uv_struct, "distance_array", []), ii),
        "distance_KF": _value(getattr(uv_struct, "distance_KF_array", []), ii),
        "G": _value(getattr(uv_struct, "G_array", []), ii),
        "G_KF": _value(getattr(uv_struct, "G_KF_array", []), ii),
    }


def _value(values: Any, ii: int) -> float | None:
    array = np.asarray(values, dtype=object).reshape(-1)
    index = max(int(ii) - 1, 0)
    if array.size <= index:
        return None
    value = array[index]
    if value is None:
        return None
    return float(np.asarray(value).reshape(-1)[0])


def _required(params: Mapping[str, Any], key: str) -> Any:
    if key not in params:
        raise KeyError(f"Missing DSCGR parameter: {key}")
    return params[key]


def _format(value: Any) -> str:
    return "nan" if value is None else f"{float(value):g}"


def _write_csv(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in CSV_FIELDS})


__all__ = ["CSV_FIELDS", "DSCGR"]
