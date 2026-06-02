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


def test_find_image_files_by_date_matches_img_png_and_sorts_by_mtime(tmp_path):
    newest = tmp_path / "IMG_003.png"
    oldest = tmp_path / "IMG_001.png"
    middle = tmp_path / "IMG_002.png"
    ignored_name = tmp_path / "ABC_001.png"
    ignored_ext = tmp_path / "IMG_004.jpg"
    _touch(newest, 300.0)
    _touch(oldest, 100.0)
    _touch(middle, 200.0)
    _touch(ignored_name, 50.0)
    _touch(ignored_ext, 75.0)

    files = find_image_files_by_date(tmp_path)

    assert files == [oldest, middle, newest]


def test_find_certain_image_file_by_date_uses_matlab_style_one_based_index(tmp_path):
    first = tmp_path / "IMG_001.png"
    second = tmp_path / "IMG_002.png"
    _touch(first, 100.0)
    _touch(second, 200.0)

    assert find_certain_image_file_by_date(tmp_path, 1) == first
    assert find_certain_image_file_by_date(tmp_path, 2) == second
    assert find_certain_image_file_by_date(tmp_path, 3) is None
    assert find_certain_image_file_by_date(tmp_path, 0) is None
