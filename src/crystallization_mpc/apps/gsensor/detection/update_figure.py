"""Translation of gsensor/detection/update_figure.m."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw


def update_figure(u_struct, v_struct, I, file, output_dir: str | Path | None = None):
    image = _to_rgb_image(I)
    draw = ImageDraw.Draw(image)

    u_color = (0, 250, 154)
    v_color = (210, 105, 30)

    _draw_dotted_line(draw, u_struct.t, u_struct.e, fill=u_color, width=1)
    _draw_dotted_line(draw, v_struct.t, v_struct.e, fill=v_color, width=1)
    draw.line(
        [_xy(u_struct.line.point1), _xy(u_struct.line.point2)],
        fill=u_color,
        width=2,
    )
    draw.line(
        [_xy(v_struct.line.point1), _xy(v_struct.line.point2)],
        fill=v_color,
        width=2,
    )

    image_dir = Path("..") / "gsensor_data" / "images" if output_dir is None else Path(output_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / _output_name(file)
    image.save(image_path)
    return image_path


def _to_rgb_image(I: Any) -> Image.Image:
    if isinstance(I, Image.Image):
        return I.convert("RGB")

    array = np.asarray(I)
    if array.dtype == bool:
        array = array.astype(np.uint8) * 255
    elif np.issubdtype(array.dtype, np.floating):
        scale = 255.0 if array.size and np.nanmax(array) <= 1.0 else 1.0
        array = np.clip(array * scale, 0, 255).astype(np.uint8)
    elif array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)

    if array.ndim == 2:
        return Image.fromarray(array).convert("RGB")
    if array.ndim == 3 and array.shape[2] == 1:
        return Image.fromarray(array[:, :, 0]).convert("RGB")
    if array.ndim == 3 and array.shape[2] >= 3:
        return Image.fromarray(array[:, :, :3]).convert("RGB")
    raise ValueError(f"Unsupported image shape: {array.shape}")


def _draw_dotted_line(
    draw: ImageDraw.ImageDraw,
    p1: Any,
    p2: Any,
    *,
    fill: tuple[int, int, int],
    width: int,
    dot_length: float = 3.0,
    gap_length: float = 3.0,
) -> None:
    start = np.asarray(_xy(p1), dtype=float)
    end = np.asarray(_xy(p2), dtype=float)
    vector = end - start
    length = float(np.linalg.norm(vector))
    if length == 0.0:
        draw.point(tuple(start), fill=fill)
        return

    direction = vector / length
    offset = 0.0
    while offset < length:
        segment_start = start + direction * offset
        segment_end = start + direction * min(offset + dot_length, length)
        draw.line(
            [tuple(segment_start), tuple(segment_end)],
            fill=fill,
            width=width,
        )
        offset += dot_length + gap_length


def _xy(point: Any) -> tuple[float, float]:
    array = np.asarray(point, dtype=float).reshape(-1)
    if array.size < 2:
        raise ValueError("Line points must contain at least x and y.")
    return float(array[0]), float(array[1])


def _output_name(file: Any) -> str:
    if isinstance(file, dict):
        name = str(file.get("name", ""))
    elif isinstance(file, (str, Path)):
        name = Path(file).name
    else:
        name = str(getattr(file, "name", ""))
    if not name:
        raise ValueError("file.name is required.")
    return name.replace(".PNG", ".jpg")


__all__ = ["update_figure"]
