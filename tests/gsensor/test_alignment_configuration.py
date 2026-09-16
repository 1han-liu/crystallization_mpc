from __future__ import annotations

from pathlib import Path
from html.parser import HTMLParser

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


def test_central_parameter_ui_renders_choice_metadata_as_select() -> None:
    source = (ROOT / "src/crystallization_mpc/apps/central/ui/static/app.js").read_text(encoding="utf-8")
    assert "Array.isArray(meta.choices)" in source
    assert 'document.createElement("select")' in source


def test_gsensor_has_dedicated_initialization_selector() -> None:
    # GSensor now uses a dedicated selector, not Central's parameter editor.
    # Its availability/draft/confirmation behavior is exercised in alignment_ui.test.cjs.
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.elements = []

        def handle_starttag(self, tag, attrs):
            self.elements.append((tag, dict(attrs)))

    document = Elements()
    document.feed((ROOT / "src/crystallization_mpc/apps/gsensor/ui/static/index.html").read_text())
    matches = [(tag, attrs) for tag, attrs in document.elements if attrs.get("id") == "alignment-method-select"]
    assert len(matches) == 1
    tag, attrs = matches[0]
    assert tag == "select" and "disabled" in attrs  # Fail closed before status/capabilities arrive.
    assert any(tag == "label" and attrs.get("for") == "alignment-method-select" for tag, attrs in document.elements)
