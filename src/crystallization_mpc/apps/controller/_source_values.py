"""Internal source-value identities retained by the numerical Controller.

The external process adapter uses the e4 transport contract. These sentinels
remain local implementation details of the retained numerical workspace.
"""
from __future__ import annotations
from enum import Enum
from typing import Any, Mapping

class MatlabSpecialValue(str, Enum):
    EMPTY = "__MATLAB_EMPTY__"
    EMPTY_CELL = "__MATLAB_EMPTY_CELL__"
    UNDEFINED = "__MATLAB_UNDEFINED__"


MATLAB_EMPTY = MatlabSpecialValue.EMPTY
MATLAB_EMPTY_CELL = MatlabSpecialValue.EMPTY_CELL
MATLAB_UNDEFINED = MatlabSpecialValue.UNDEFINED

def is_matlab_empty(value: Any) -> bool:
    return (value is MATLAB_EMPTY or isinstance(value, (list, tuple)) and not value
            or getattr(value, "size", None) == 0)


def matlab_value_document(value: Any) -> Any:
    if value is MATLAB_EMPTY:
        return {"__matlab_value__": "empty"}
    if value is MATLAB_UNDEFINED:
        return {"__matlab_value__": "undefined"}
    if value is MATLAB_EMPTY_CELL:
        return {"__matlab_value__": "empty_cell"}
    if isinstance(value, Mapping):
        return {key: matlab_value_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [matlab_value_document(item) for item in value]
    if hasattr(value, "tolist"):
        return matlab_value_document(value.tolist())
    return value



class SourceGrowthSample(dict):
    """Observation view, without a second G numeric admission policy."""
    def __getattr__(self, name):
        return self.get(name)

    def to_dict(self):
        return dict(self)
