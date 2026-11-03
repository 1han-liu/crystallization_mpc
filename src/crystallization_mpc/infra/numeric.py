"""Lossless JSON representation of IEEE non-finite measurement values.

This is a transport codec, not an input policy: it never substitutes a missing,
zero, previous, or predicted value. Command identities/limits use their own
contracts. Telegraf's flat field serializer uses the reserved string tokens;
Python JSON documents use explicit single-key tagged objects.
"""
from __future__ import annotations

import math
from numbers import Real
from typing import Any

FLOAT_TAG = "__mpcrystal_float__"
FLOAT_PREFIX = "@mpcrystal:float:"
_VALUES = {"NaN": float("nan"), "+Inf": float("inf"), "-Inf": float("-inf")}


def nonfinite_symbol(value: Any) -> str | None:
    if not isinstance(value, Real) or isinstance(value, bool):
        return None
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    return None


def encode_nonfinite(value: Any) -> Any:
    symbol = nonfinite_symbol(value)
    if symbol is not None:
        return {FLOAT_TAG: symbol}
    if isinstance(value, dict):
        return {key: encode_nonfinite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_nonfinite(item) for item in value]
    return value


def decode_nonfinite(value: Any) -> Any:
    if isinstance(value, dict):
        if (set(value) == {FLOAT_TAG} and isinstance(value[FLOAT_TAG], str)
                and value[FLOAT_TAG] in _VALUES):
            return _VALUES[value[FLOAT_TAG]]
        return {key: decode_nonfinite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_nonfinite(item) for item in value]
    return value


def decode_numeric(value: Any, name: str = "measurement") -> float:
    value = decode_nonfinite(value)
    if isinstance(value, str) and value.startswith(FLOAT_PREFIX):
        symbol = value[len(FLOAT_PREFIX):]
        if symbol in _VALUES:
            return _VALUES[symbol]
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be numeric.")
    return float(value)


def influx_numeric_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Keep Influx field types stable and explicitly archive non-finite values.

    ``T_nonfinite=\"NaN\"`` represents the original numeric T value. It is
    neither an omitted observation nor a substituted finite T measurement.
    """
    result: dict[str, Any] = {}
    for name, value in fields.items():
        symbol = nonfinite_symbol(value)
        if symbol is None:
            result[name] = value
        else:
            result[f"{name}_nonfinite"] = symbol
    return result


def restore_influx_numeric_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Restore an archived non-finite measurement from its companion field."""
    result = dict(fields)
    for name, value in fields.items():
        if name.endswith("_nonfinite") and isinstance(value, str) and value in _VALUES:
            result[name[:-len("_nonfinite")]] = _VALUES[value]
            del result[name]
    return result
