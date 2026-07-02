import os

from crystallization_mpc.apps.gsensor.utils.find_certain_image_file_by_date import (
    find_certain_image_file_by_date,
)
from crystallization_mpc.apps.gsensor.utils.find_image_files_by_date import (
    find_image_files_by_date,
)


def _touch(path, timestamp):
    path.write_bytes(b"")
    os.utime(path, (timestamp, timestamp))


def test_find_image_files_by_date_matches_supported_images_and_sorts_by_mtime(tmp_path):
    newest = tmp_path / "frame_003.jpeg"
    oldest = tmp_path / "image_001.jpg"
    middle = tmp_path / "IMG_002.png"
    tif = tmp_path / "capture_004.tif"
    ignored_ext = tmp_path / "notes.txt"
    _touch(newest, 300.0)
    _touch(oldest, 100.0)
    _touch(middle, 200.0)
    _touch(tif, 400.0)
    _touch(ignored_ext, 50.0)

    files = find_image_files_by_date(tmp_path)

    assert files == [oldest, middle, newest, tif]


def test_find_certain_image_file_by_date_uses_matlab_style_one_based_index(tmp_path):
    first = tmp_path / "IMG_001.png"
    second = tmp_path / "IMG_002.png"
    _touch(first, 100.0)
    _touch(second, 200.0)

    assert find_certain_image_file_by_date(tmp_path, 1) == first
    assert find_certain_image_file_by_date(tmp_path, 2) == second
    assert find_certain_image_file_by_date(tmp_path, 3) is None
    assert find_certain_image_file_by_date(tmp_path, 0) is None
