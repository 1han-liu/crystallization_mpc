"""Logical conversion at the original MATLAB if, ~ and && expressions."""
from __future__ import annotations

from typing import Any
import numpy as np

from crystallization_mpc.apps.controller.process import (
    MATLAB_EMPTY, MATLAB_EMPTY_CELL, MATLAB_UNDEFINED,
)
class MatlabCellValues(list):
    """Typed cell contents retained for numerical logical conversion only."""


class SourceConditionalError(ValueError):
    def __init__(self, identifier: str):
        self.reference_identifier = identifier
        super().__init__(identifier)


def source_logical(value: Any) -> np.ndarray:
    """The logical conversion itself, before if-reduction or scalar &&."""
    if value is MATLAB_UNDEFINED:
        raise SourceConditionalError("MATLAB:UndefinedFunction")
    if value is MATLAB_EMPTY_CELL or isinstance(value, (MatlabCellValues, dict)):
        raise SourceConditionalError("MATLAB:invalidConversion")
    if value is MATLAB_EMPTY:
        value = []
    if isinstance(value, str):
        array = np.asarray([ord(char) for char in value])
    else:
        array = np.asarray(value)
    if np.iscomplexobj(array):
        raise SourceConditionalError("MATLAB:nologicalcomplex")
    if array.dtype.kind not in "biuf":
        raise SourceConditionalError("MATLAB:invalidConversion")
    if np.isnan(array).any():
        raise SourceConditionalError("MATLAB:nologicalnan")
    return array != 0


def source_if(value: Any) -> bool:
    logical = source_logical(value)
    return logical.size != 0 and bool(np.all(logical))


def source_short_circuit(value: Any) -> bool:
    logical = source_logical(value)
    if logical.size != 1:
        raise SourceConditionalError("MATLAB:nonLogicalConditional")
    return bool(logical.reshape(-1)[0])


def source_not(value: Any) -> np.ndarray:
    return np.logical_not(source_logical(value))
