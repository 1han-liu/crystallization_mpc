from __future__ import annotations

import sys

import numpy
import scipy


def test_controller_validation_environment() -> None:
    assert sys.version_info[:2] == (3, 12)
    assert numpy.__version__ == "2.5.2"
    assert scipy.__version__ == "1.18.1"
