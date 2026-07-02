"""Translation of gsensor/utils/find_image_files_by_date.m."""

from pathlib import Path


SUPPORTED_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
}


def find_image_files_by_date(folder):
    folder_path = Path(folder).expanduser()
    files = [
        path
        for path in folder_path.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
    ]
    return sorted(files, key=lambda path: path.stat().st_mtime)


__all__ = ["SUPPORTED_IMAGE_SUFFIXES", "find_image_files_by_date"]
