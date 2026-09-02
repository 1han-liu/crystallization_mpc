from __future__ import annotations

import pytest

from crystallization_mpc.apps.controller.result import ControllerStepResult


def test_valid_result_requires_real_jacket_setpoint() -> None:
    with pytest.raises(ValueError, match="requires a calculated T_j_set"):
        ControllerStepResult(sigma=0.12)


def test_valid_result_with_finite_setpoint_is_accepted() -> None:
    result = ControllerStepResult(T_j_set=300.0, sigma=0.12)
    assert result.valid
    assert result.T_j_set == 300.0


def test_invalid_result_has_only_error_and_null_numerics() -> None:
    result = ControllerStepResult(valid=False, error="temporary optimizer failure")
    assert not result.valid
    assert all(getattr(result, field) is None for field in result.NUMERIC_FIELDS)
