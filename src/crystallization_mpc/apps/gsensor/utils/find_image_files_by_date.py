"""Translation of gsensor/utils/find_image_files_by_date.m."""

from pathlib import Path


def find_image_files_by_date(folder):
    folder_path = Path(folder).expanduser()
    files = [path for path in folder_path.glob("IMG_*.png") if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime)


__all__ = ["find_image_files_by_date"]
