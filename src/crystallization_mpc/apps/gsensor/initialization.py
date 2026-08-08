from __future__ import annotations

import math
import mimetypes
import struct
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np

from crystallization_mpc.apps.gsensor.morphs.calc_foot_point import calc_foot_point
from crystallization_mpc.apps.gsensor.morphs.calc_intersect import calc_intersect
from crystallization_mpc.apps.gsensor.morphs.calc_normal import calc_normal
from crystallization_mpc.apps.gsensor.morphs.recover_3d_all import recover_3d_all
from crystallization_mpc.apps.gsensor.utils.reorient_points import reorient_points

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
CORNER_CHOICES = {"A", "B", "C"}
GEOMETRY_EPSILON = 1.0e-9


@dataclass
class InitializationSession:
    session_id: str
    image_folder: str
    images: list[str]
    selected_image: str
    image_width: int | None = None
    image_height: int | None = None
    is_full: bool | None = None
    corner: str | None = None
    candidates_3d: list[dict[str, Any]] = field(default_factory=list)
    selected_3d_choice: int | None = None
    recovered_3d: dict[str, Any] | None = None
    points: dict[str, list[float]] = field(default_factory=dict)
    point_history: list[str] = field(default_factory=list)
    status: str = "awaiting_is_full"


class GsensorInitializationManager:
    def __init__(self) -> None:
        self.sessions: dict[str, InitializationSession] = {}
        self.active_session_id: str | None = None

    def start_folder(self, folder: str, image_choice: str = "first") -> dict[str, Any]:
        folder_path = Path(folder).expanduser().resolve(strict=False)
        if not folder_path.is_dir():
            raise FileNotFoundError(f"Image folder not found: {folder_path}")
        images = list_supported_images(folder_path)
        if not images:
            raise ValueError(f"No supported images found in: {folder_path}")
        if image_choice not in {"first", "latest"}:
            raise ValueError("image_choice must be first or latest.")

        selected_image = images[-1] if image_choice == "latest" else images[0]
        image_width, image_height = read_image_size(selected_image)
        session = InitializationSession(
            session_id=uuid4().hex,
            image_folder=str(folder_path),
            images=[str(path) for path in images],
            selected_image=str(selected_image),
            image_width=image_width,
            image_height=image_height,
        )
        self.sessions[session.session_id] = session
        self.active_session_id = session.session_id
        return self.payload(session.session_id)

    def payload(self, session_id: str | None = None) -> dict[str, Any]:
        session = self._get_session(session_id, required=False)
        if session is None:
            return {
                "session_id": None,
                "status": "not_started",
                "current_step": None,
                "points": {},
                "derived": {},
                "overlays": [],
                "candidates_3d": [],
                "selected_3d_choice": None,
                "recovered_3d": None,
                "can_undo": False,
            }
        derived = self._derived(session)
        current_step = self._current_step(session)
        return {
            "session_id": session.session_id,
            "status": session.status,
            "image_folder": session.image_folder,
            "images": session.images,
            "selected_image": session.selected_image,
            "image_width": session.image_width,
            "image_height": session.image_height,
            "is_full": session.is_full,
            "corner": session.corner,
            "current_step": current_step,
            "points": {key: point[:2] for key, point in session.points.items()},
            "derived": {key: point[:2] for key, point in derived["points"].items()},
            "overlays": self._overlays(session, derived),
            "candidates_3d": session.candidates_3d,
            "selected_3d_choice": session.selected_3d_choice,
            "recovered_3d": session.recovered_3d,
            "can_undo": bool(session.point_history or session.corner is not None),
        }

    def image_path(self, session_id: str) -> Path:
        session = self._get_session(session_id)
        return Path(session.selected_image)

    def image_media_type(self, session_id: str) -> str:
        path = self.image_path(session_id)
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def set_is_full(self, session_id: str, is_full: bool) -> dict[str, Any]:
        session = self._get_session(session_id)
        session.is_full = bool(is_full)
        session.corner = None
        session.candidates_3d.clear()
        session.selected_3d_choice = None
        session.recovered_3d = None
        session.points.clear()
        session.point_history.clear()
        session.status = "marking_points"
        return self.payload(session.session_id)

    def submit_point(self, session_id: str, x: float, y: float) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.is_full is None:
            raise ValueError("Choose full mode before marking points.")
        self._validate_point(session, x, y)
        current_step = self._current_step(session)
        if not current_step or current_step.get("type") != "point":
            raise ValueError("The current initialization step does not accept a point.")

        key = current_step["key"]
        session.points[key] = [float(x), float(y), 0.0]
        session.point_history.append(key)
        self._normalize_oriented_sides(session)
        try:
            self._validate_geometry(session)
            self._update_status(session)
            return self.payload(session.session_id)
        except ValueError:
            if session.point_history and session.point_history[-1] == key:
                session.point_history.pop()
            session.points.pop(key, None)
            self._update_status(session)
            raise

    def choose_corner(self, session_id: str, corner: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.status not in {"ready_for_corner", "ready_for_3d_choice", "ready_for_3d"}:
            raise ValueError("Complete 2D point marking before choosing a corner.")
        corner = corner.upper()
        if corner not in CORNER_CHOICES:
            raise ValueError("corner must be A, B, or C.")
        session.corner = corner
        session.candidates_3d = self._generate_3d_candidates(session)
        session.selected_3d_choice = None
        session.recovered_3d = None
        session.status = "ready_for_3d_choice"
        return self.payload(session.session_id)

    def select_3d_choice(self, session_id: str, choice: int) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.status not in {"ready_for_3d_choice", "ready_for_3d"}:
            raise ValueError("Choose a corner before selecting a 3D candidate.")
        for candidate in session.candidates_3d:
            if candidate["choice"] == choice:
                session.selected_3d_choice = choice
                session.recovered_3d = {
                    "M": candidate["M"],
                    "W": candidate["W"],
                    "U": candidate["U"],
                    "V": candidate["V"],
                    "show_3d": candidate.get("show_3d"),
                }
                session.status = "ready_for_3d"
                return self.payload(session.session_id)
        raise ValueError(f"Unknown 3D choice: {choice}")

    def _generate_3d_candidates(self, session: InitializationSession) -> list[dict[str, Any]]:
        derived = self._derived(session)
        points = derived["points"]
        missing = [key for key in ("m", "w", "u", "v") if key not in points]
        if missing:
            raise ValueError(
                "Initialization geometry is incomplete for 3D recovery: "
                + ", ".join(missing)
            )
        candidates = recover_3d_all(
            None,
            points["m"],
            points["w"],
            points["u"],
            points["v"],
            session.corner,
            session.is_full,
        )
        return [candidate.as_dict() for candidate in candidates]

    def undo(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.selected_3d_choice is not None or session.recovered_3d is not None:
            session.selected_3d_choice = None
            session.recovered_3d = None
            session.status = "ready_for_3d_choice"
            return self.payload(session.session_id)
        if session.corner is not None:
            session.corner = None
            session.candidates_3d.clear()
            session.selected_3d_choice = None
            session.recovered_3d = None
            session.status = "ready_for_corner"
            return self.payload(session.session_id)
        if session.point_history:
            key = session.point_history.pop()
            session.points.pop(key, None)
            self._update_status(session)
        return self.payload(session.session_id)

    def reset(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is None:
            # No session id means service-level cleanup rather than the UI's
            # in-session reset action.
            self.sessions.pop(self.active_session_id, None)
            self.active_session_id = None
            return self.payload(None)

        session = self._get_session(session_id)
        session.is_full = None
        session.corner = None
        session.candidates_3d.clear()
        session.selected_3d_choice = None
        session.recovered_3d = None
        session.points.clear()
        session.point_history.clear()
        self.active_session_id = session.session_id
        self._update_status(session)
        return self.payload(session.session_id)

    def _get_session(
        self,
        session_id: str | None,
        *,
        required: bool = True,
    ) -> InitializationSession | None:
        target_session_id = session_id or self.active_session_id
        if target_session_id is None:
            if required:
                raise ValueError("No active initialization session.")
            return None
        session = self.sessions.get(target_session_id)
        if session is None and (required or session_id is not None):
            raise ValueError(f"Unknown initialization session: {target_session_id}")
        return session

    def _validate_point(self, session: InitializationSession, x: float, y: float) -> None:
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Point coordinates must be finite numbers.")
        if x < 0 or y < 0:
            raise ValueError("Point coordinates must be non-negative.")
        if session.image_width is not None and x > session.image_width:
            raise ValueError("Point x coordinate is outside the image.")
        if session.image_height is not None and y > session.image_height:
            raise ValueError("Point y coordinate is outside the image.")

    def _validate_geometry(self, session: InitializationSession) -> None:
        for prefix in ("u_ad", "v_ad", "u_op", "v_op"):
            t_key = f"{prefix}.t"
            e_key = f"{prefix}.e"
            if t_key in session.points and e_key in session.points:
                t = point_array(session.points[t_key])
                e = point_array(session.points[e_key])
                if np.linalg.norm(e - t) <= GEOMETRY_EPSILON:
                    raise ValueError(f"Side {prefix} requires two distinct points.")
        try:
            derived = self._derived(session)
        except np.linalg.LinAlgError as exc:
            raise ValueError(
                "Initialization geometry is degenerate; check that marked side lines intersect."
            ) from exc
        for point in derived["points"].values():
            if not np.all(np.isfinite(point_array(point))):
                raise ValueError("Initialization geometry produced a non-finite point.")
        for side in derived["sides"].values():
            for attr in ("t", "e", "n", "v", "vc"):
                if not np.all(np.isfinite(point_array(getattr(side, attr)))):
                    raise ValueError("Initialization geometry produced a non-finite side.")

    def _current_step(self, session: InitializationSession) -> dict[str, Any] | None:
        if session.is_full is None:
            return {
                "type": "choice",
                "key": "is_full",
                "prompt": "Choose whether the crystal is full mode.",
            }

        for step in self._point_steps(session):
            if step["key"] not in session.points:
                return step
        if session.corner is None:
            return {
                "type": "corner",
                "key": "corner",
                "prompt": "Choose corner A, B, or C.",
                "choices": sorted(CORNER_CHOICES),
            }
        if session.selected_3d_choice is None:
            return {
                "type": "3d_choice",
                "key": "3d_choice",
                "prompt": "Choose a 3D candidate.",
                "choices": [candidate["choice"] for candidate in session.candidates_3d],
            }
        return None

    def _point_steps(self, session: InitializationSession) -> list[dict[str, str]]:
        steps = []
        steps.extend(side_steps("u", "small", "adjacent"))
        steps.extend(side_steps("v", "large", "adjacent"))
        if session.is_full:
            steps.extend(side_steps("u", "small", "opposite"))
            steps.extend(side_steps("v", "large", "opposite"))
        else:
            steps.append(point_step("w", "middle", "w", ""))
        for index in range(1, 5):
            steps.append(
                point_step(
                    f"kernel.k_c_cell.{index}",
                    f"{index}.",
                    "k_c",
                    "kernel corners",
                    label=f"k_c_{index}",
                )
            )
            steps.append(
                point_step(
                    f"kernel.k_o_cell.{index}",
                    f"{index}.",
                    "k_o",
                    "kernel outer points",
                    label=f"k_o_{index}",
                )
            )
        return steps

    def _normalize_oriented_sides(self, session: InitializationSession) -> None:
        for prefix in ("u_ad", "v_ad", "u_op", "v_op"):
            t_key = f"{prefix}.t"
            e_key = f"{prefix}.e"
            if t_key in session.points and e_key in session.points:
                t, e = reorient_points(session.points[t_key], session.points[e_key])
                session.points[t_key] = point_list(t)
                session.points[e_key] = point_list(e)

    def _update_status(self, session: InitializationSession) -> None:
        if session.is_full is None:
            session.status = "awaiting_is_full"
        elif any(step["key"] not in session.points for step in self._point_steps(session)):
            session.status = "marking_points"
        elif session.corner is None:
            session.status = "ready_for_corner"
        elif session.selected_3d_choice is None:
            session.status = "ready_for_3d_choice"
        else:
            session.status = "ready_for_3d"

    def _derived(self, session: InitializationSession) -> dict[str, Any]:
        points: dict[str, list[float]] = {}
        sides: dict[str, SimpleNamespace] = {}
        for prefix, foot, suffix in (
            ("u_ad", "u", "adjacent"),
            ("v_ad", "v", "adjacent"),
            ("u_op", "u", "opposite"),
            ("v_op", "v", "opposite"),
        ):
            side = self._side_struct(session, prefix, foot, suffix)
            if side is not None:
                sides[prefix] = side

        if "u_ad" in sides and "v_ad" in sides:
            m = calc_intersect(
                sides["u_ad"].t,
                sides["u_ad"].e,
                sides["v_ad"].t,
                sides["v_ad"].e,
            )
            points["m"] = point_list(m)
            if session.is_full:
                if "u_op" in sides:
                    points["u"] = point_list(
                        calc_intersect(
                            sides["u_op"].t,
                            sides["u_op"].e,
                            sides["u_ad"].t,
                            sides["u_ad"].e,
                        )
                    )
                if "v_op" in sides:
                    points["v"] = point_list(
                        calc_intersect(
                            sides["v_op"].t,
                            sides["v_op"].e,
                            sides["v_ad"].t,
                            sides["v_ad"].e,
                        )
                    )
                if "u_op" in sides and "v_op" in sides:
                    points["w"] = point_list(
                        calc_intersect(
                            sides["u_op"].t,
                            sides["u_op"].e,
                            sides["v_op"].t,
                            sides["v_op"].e,
                        )
                    )
            else:
                points["u"] = point_list(calc_foot_point(points["m"], sides["u_ad"]))
                points["v"] = point_list(calc_foot_point(points["m"], sides["v_ad"]))
                if "w" in session.points:
                    points["w"] = session.points["w"]

        return {"points": points, "sides": sides}

    def _side_struct(
        self,
        session: InitializationSession,
        prefix: str,
        foot: str,
        suffix: str,
    ) -> SimpleNamespace | None:
        t_key = f"{prefix}.t"
        e_key = f"{prefix}.e"
        o_key = f"{prefix}.o"
        if t_key not in session.points or e_key not in session.points:
            return None
        t, e = reorient_points(session.points[t_key], session.points[e_key])
        n, v, vc = calc_normal(t, e)
        kwargs = {
            "t": point_array(t),
            "e": point_array(e),
            "n": point_array(n),
            "v": point_array(v),
            "vc": point_array(vc),
            "foot": foot,
            "suffix": suffix,
        }
        if o_key in session.points:
            kwargs["o"] = point_array(session.points[o_key])
        return SimpleNamespace(**kwargs)

    def _overlays(
        self,
        session: InitializationSession,
        derived: dict[str, Any],
    ) -> list[dict[str, Any]]:
        overlays: list[dict[str, Any]] = []
        sides = derived["sides"]
        for prefix, side in sides.items():
            add_line(overlays, side.t, side.e, role="side")
            add_arrow(overlays, side.t, side.e, role="side_direction")
            add_arrow(overlays, side.vc, side.v, role="normal")
        for key, point in session.points.items():
            add_point(overlays, point, label=point_label(key), role=point_role(key))
        for key, point in derived["points"].items():
            add_point(overlays, point, label=key, role="computed")

        derived_points = derived["points"]
        if "m" in derived_points:
            for key in ("u", "v", "w"):
                if key in derived_points:
                    add_line(overlays, derived_points["m"], derived_points[key], role="derived")
        if session.is_full and "u" in derived_points and "w" in derived_points:
            add_line(overlays, derived_points["u"], derived_points["w"], role="derived")
        if session.is_full and "v" in derived_points and "w" in derived_points:
            add_line(overlays, derived_points["v"], derived_points["w"], role="derived")

        kernel_corners = [
            session.points[f"kernel.k_c_cell.{index}"]
            for index in range(1, 5)
            if f"kernel.k_c_cell.{index}" in session.points
        ]
        for left, right in zip(kernel_corners, kernel_corners[1:]):
            add_line(overlays, left, right, role="kernel")
        if len(kernel_corners) == 4:
            add_line(overlays, kernel_corners[-1], kernel_corners[0], role="kernel")
        return overlays


def side_steps(foot: str, angle: str, suffix: str) -> list[dict[str, str]]:
    side_short = suffix[0:2]
    side_name = f"{foot}_{side_short}"
    text_add = f"{angle} angle at {suffix} side"
    return [
        point_step(f"{side_name}.t", "first", f"t_{side_name}", text_add),
        point_step(f"{side_name}.e", "second", f"e_{side_name}", text_add),
        point_step(f"{side_name}.o", "outer", f"o_{side_name}", text_add),
    ]


def point_step(
    key: str,
    order: str,
    point_name: str,
    text_add: str,
    *,
    label: str | None = None,
) -> dict[str, str]:
    suffix = f" of {text_add}" if text_add else ""
    return {
        "type": "point",
        "key": key,
        "label": label or point_name,
        "prompt": f"Mark {order} point ({point_name}){suffix} in 2D.",
    }


def point_label(key: str) -> str:
    if key == "w":
        return "w"
    if key.startswith("kernel.k_c_cell."):
        return "k_c_" + key.rsplit(".", 1)[-1]
    if key.startswith("kernel.k_o_cell."):
        return "k_o_" + key.rsplit(".", 1)[-1]
    return key.replace(".", "_")


def point_role(key: str) -> str:
    if key.startswith("kernel.k_c_cell."):
        return "kernel_corner"
    if key.startswith("kernel.k_o_cell."):
        return "kernel_outer"
    return "manual"


def add_point(overlays: list[dict[str, Any]], point: Any, *, label: str, role: str) -> None:
    point = point_list(point)
    overlays.append(
        {
            "type": "point",
            "x": point[0],
            "y": point[1],
            "label": label,
            "role": role,
        }
    )


def add_line(overlays: list[dict[str, Any]], p1: Any, p2: Any, *, role: str) -> None:
    left = point_list(p1)
    right = point_list(p2)
    overlays.append(
        {
            "type": "line",
            "x1": left[0],
            "y1": left[1],
            "x2": right[0],
            "y2": right[1],
            "role": role,
        }
    )


def add_arrow(overlays: list[dict[str, Any]], p1: Any, p2: Any, *, role: str) -> None:
    left = point_list(p1)
    right = point_list(p2)
    overlays.append(
        {
            "type": "arrow",
            "x1": left[0],
            "y1": left[1],
            "x2": right[0],
            "y2": right[1],
            "role": role,
        }
    )


def point_array(point: Any) -> np.ndarray:
    array = np.asarray(point, dtype=float).reshape(-1)
    if array.size == 2:
        array = np.array([array[0], array[1], 0.0])
    return array


def point_list(point: Any) -> list[float]:
    array = point_array(point)
    return [float(array[0]), float(array[1]), float(array[2])]


def list_supported_images(folder: str | Path) -> list[Path]:
    """Return directly contained initialization images in deterministic name order."""

    folder_path = Path(folder).expanduser().resolve(strict=False)
    if not folder_path.is_dir():
        raise FileNotFoundError(f"Image folder not found: {folder_path}")
    return sorted(
        [
            path
            for path in folder_path.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ],
        key=lambda path: path.name,
    )


def read_image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        with path.open("rb") as file:
            header = file.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                width, height = struct.unpack(">II", header[16:24])
                return int(width), int(height)
            if header.startswith(b"BM") and len(header) >= 26:
                width, height = struct.unpack("<ii", header[18:26])
                return int(abs(width)), int(abs(height))
            if header.startswith((b"GIF87a", b"GIF89a")) and len(header) >= 10:
                width, height = struct.unpack("<HH", header[6:10])
                return int(width), int(height)
    except OSError:
        return None, None
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        return read_jpeg_size(path)
    return None, None


def read_jpeg_size(path: Path) -> tuple[int | None, int | None]:
    try:
        with path.open("rb") as file:
            if file.read(2) != b"\xff\xd8":
                return None, None
            while True:
                byte = file.read(1)
                if not byte:
                    return None, None
                if byte != b"\xff":
                    continue
                marker = file.read(1)
                while marker == b"\xff":
                    marker = file.read(1)
                if marker in {b"\xd8", b"\xd9"}:
                    continue
                length_bytes = file.read(2)
                if len(length_bytes) != 2:
                    return None, None
                length = struct.unpack(">H", length_bytes)[0]
                if marker in {
                    b"\xc0",
                    b"\xc1",
                    b"\xc2",
                    b"\xc3",
                    b"\xc5",
                    b"\xc6",
                    b"\xc7",
                    b"\xc9",
                    b"\xca",
                    b"\xcb",
                    b"\xcd",
                    b"\xce",
                    b"\xcf",
                }:
                    data = file.read(5)
                    if len(data) != 5:
                        return None, None
                    height, width = struct.unpack(">HH", data[1:5])
                    return int(width), int(height)
                file.seek(length - 2, 1)
    except OSError:
        return None, None


__all__ = [
    "GsensorInitializationManager",
    "InitializationSession",
    "list_supported_images",
    "read_image_size",
]
