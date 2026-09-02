from __future__ import annotations

from pathlib import Path

import pytest

from crystallization_mpc.apps.central.params import (
    ParameterValidationError,
    load_param_meta,
    load_params,
    validate_params_section,
)
from crystallization_mpc.apps.gsensor.alignment import ALIGNMENT_METHODS
from crystallization_mpc.apps.gsensor.app import get_alignment_capabilities


ROOT = Path(__file__).resolve().parents[2]


def test_alignment_parameter_is_an_enum_with_none_default() -> None:
    _shared, gsensor, _controller, _version = load_params(str(ROOT / "params_default.yaml"))
    meta = load_param_meta(str(ROOT / "param_meta.yaml"))
    assert gsensor["alignment_method"] == "none"
    assert tuple(meta["alignment_method"]["choices"]) == ALIGNMENT_METHODS
    assert meta["alignment_method"]["ui"]["control"] == "select"
    with pytest.raises(ParameterValidationError, match="must be one of"):
        validate_params_section(
            "gsensor",
            {**gsensor, "alignment_method": "unknown"},
            gsensor,
            meta,
        )


def test_capability_endpoint_reports_every_configured_method() -> None:
    payload = get_alignment_capabilities()
    methods = payload["methods"]
    assert tuple(item["method"] for item in methods) == ALIGNMENT_METHODS
    assert all(isinstance(item["available"], bool) for item in methods)


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/crystallization_mpc/apps/central/ui/static/app.js",
        "src/crystallization_mpc/apps/gsensor/ui/static/app.js",
    ],
)
def test_parameter_ui_renders_choice_metadata_as_select(relative_path: str) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    assert "Array.isArray(meta.choices)" in source
    assert 'document.createElement("select")' in source
