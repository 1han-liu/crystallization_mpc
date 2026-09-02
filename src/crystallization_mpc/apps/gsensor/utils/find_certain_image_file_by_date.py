"""Translation of gsensor/utils/find_certain_image_file_by_date.m."""

from crystallization_mpc.apps.gsensor.utils.find_image_files_by_date import (
    find_image_files_by_date,
)


def find_certain_image_file_by_date(folder, ptr):
    next_image_file = None
    image_files = find_image_files_by_date(folder)
    try:
        index = int(ptr) - 1
        if index >= 0:
            next_image_file = image_files[index]
    except (IndexError, TypeError, ValueError):
        pass
    return next_image_file


__all__ = ["find_certain_image_file_by_date"]
