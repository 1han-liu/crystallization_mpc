"""Locate the pinned Python MKL distribution on Windows and Linux x86-64."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import platform
import struct
import sys
import sysconfig
from typing import Any


def runtime_layout(
    system: str, machine: str, pointer_size: int, prefix: Path, data_prefix: Path,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Use wheel installation paths, without searching PATH or MATLAB."""
    if (system not in {"linux", "win32"} or pointer_size != 8
            or machine.lower() not in {"x86_64", "amd64"}):
        raise ImportError("Controller numerics require 64-bit Python on Windows or Linux x86-64.")
    if system == "linux":
        return (prefix / "lib" / "libmkl_rt.so.2",), ()
    roots = tuple(dict.fromkeys((data_prefix, prefix)))
    # Intel's win_amd64 wheel installs its runtime and companion DLLs in
    # <data>/Library/bin. Scripts is also a Python distribution DLL location.
    directories = tuple(dict.fromkeys(
        root / relative for root in roots for relative in ("Library/bin", "Scripts")
    ))
    return tuple(path / "mkl_rt.2.dll" for path in directories), directories


def load_mkl_runtime() -> tuple[Path, Any, tuple[Any, ...]]:
    candidates, directories = runtime_layout(
        sys.platform, platform.machine(), struct.calcsize("P"),
        Path(sys.prefix), Path(sysconfig.get_path("data")),
    )
    library_path = next((path for path in candidates if path.is_file()), None)
    if library_path is None:
        searched = ", ".join(str(path) for path in candidates)
        raise ImportError(
            "Controller numerics require mkl==2025.0.1 in this Python environment. "
            "Install with: python -m pip install -r requirements.txt. "
            f"Runtime not found at: {searched}"
        )
    handles = []
    try:
        for directory in directories:
            if directory.is_dir():
                handles.append(os.add_dll_directory(str(directory)))
        library = ctypes.CDLL(str(library_path))
    except OSError as exc:
        for handle in reversed(handles):
            handle.close()
        raise ImportError(
            f"Could not load {library_path}. Reinstall mkl==2025.0.1 and its "
            "dependencies in this Python environment."
        ) from exc
    # Keep AddDllDirectory handles alive: MKL loads further kernels lazily.
    return library_path, library, tuple(handles)
