from pathlib import Path

import pytest

from crystallization_mpc.apps.central.params import load_params
from crystallization_mpc.apps.central.params import load_param_meta
from crystallization_mpc.apps.gsensor.telemetry import (
    GSENSOR_PARAMS_MEASUREMENT,
    GsensorParamRecord,
    PARAM_EVENT_STARTUP_SNAPSHOT,
    PARAM_SCOPE_GSENSOR,
    PARAM_SCOPE_SHARED,
    PARAM_SOURCE_DEFAULT,
    build_gsensor_param_point,
    iter_gsensor_param_records,
    param_value_field,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_gsensor_param_records_use_default_yaml_keys_and_scopes():
    shared, gsensor, controller, version = load_params(str(PROJECT_ROOT / "params_default.yaml"))

    records = list(
        iter_gsensor_param_records(
            shared,
            gsensor,
            source=PARAM_SOURCE_DEFAULT,
            event=PARAM_EVENT_STARTUP_SNAPSHOT,
            version=version,
            run_id="test-run",
        )
    )

    records_by_key = {record.param_key: record for record in records}
    assert set(records_by_key) == set(shared) | set(gsensor)
    assert not (set(records_by_key) & set(controller))
    assert records_by_key["dt_G"].scope == PARAM_SCOPE_SHARED
    assert records_by_key["params_G.width"].scope == PARAM_SCOPE_GSENSOR
    assert records_by_key["ptr_format"].param_key == "ptr_format"


def test_image_folder_is_not_a_published_gsensor_param():
    _shared, gsensor, _controller, _version = load_params(str(PROJECT_ROOT / "params_default.yaml"))
    meta = load_param_meta(str(PROJECT_ROOT / "param_meta.yaml"))

    assert "image_folder" not in gsensor
    assert "image_folder" not in meta


def test_gsensor_param_record_serializes_values_by_type():
    numeric = GsensorParamRecord(
        param_key="params_G.width",
        value=100,
        scope=PARAM_SCOPE_GSENSOR,
        source=PARAM_SOURCE_DEFAULT,
        event=PARAM_EVENT_STARTUP_SNAPSHOT,
    )
    text_list = GsensorParamRecord(
        param_key="ptr_format",
        value=["%d", "%05d", "%d"],
        scope=PARAM_SCOPE_GSENSOR,
        source=PARAM_SOURCE_DEFAULT,
        event=PARAM_EVENT_STARTUP_SNAPSHOT,
        changed=False,
    )

    assert numeric.tags()["param_key"] == "params_G.width"
    assert numeric.tags()["value_type"] == "float"
    assert numeric.fields()["value_float"] == 100.0
    assert numeric.fields()["changed"] is True
    assert text_list.tags()["value_type"] == "json"
    assert text_list.fields()["value_json"] == '["%d","%05d","%d"]'
    assert text_list.fields()["changed"] is False


def test_param_value_field_keeps_yaml_param_names_out_of_field_names():
    field_name, field_value = param_value_field("Ziegler_Nichols")

    assert field_name == "value_string"
    assert field_value == "Ziegler_Nichols"


def test_build_gsensor_param_point_uses_measurement_and_tags():
    pytest.importorskip("influxdb_client")
    record = GsensorParamRecord(
        param_key="dt_G",
        value=15,
        scope=PARAM_SCOPE_SHARED,
        source=PARAM_SOURCE_DEFAULT,
        event=PARAM_EVENT_STARTUP_SNAPSHOT,
        version=1,
        run_id="run-001",
    )

    line = build_gsensor_param_point(record).to_line_protocol()

    assert line.startswith(GSENSOR_PARAMS_MEASUREMENT)
    assert "param_key=dt_G" in line
    assert "scope=shared" in line
    assert "value_float=15" in line
