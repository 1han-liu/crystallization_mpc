"""Observe file replacement and edits without reading document contents."""
from pathlib import Path


def file_version(path: str | Path) -> tuple[int, ...] | None:
    try:
        info = Path(path).stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
