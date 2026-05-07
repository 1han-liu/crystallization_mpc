from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from crystallization_mpc.infra.influxdb.write import InfluxWriter, build_tagged_point

GSENSOR_PARAMS_MEASUREMENT = "gsensor_params"
GSENSOR_SERVICE_TAG = "gsensor"

PARAM_SOURCE_DEFAULT = "default"
PARAM_SOURCE_CENTRAL = "central"
PARAM_SOURCE_UI = "ui"
PARAM_SOURCE_RUNTIME = "runtime"

PARAM_EVENT_STARTUP_SNAPSHOT = "startup_snapshot"
PARAM_EVENT_CENTRAL_UPDATE = "central_update"
PARAM_EVENT_UI_APPLY = "ui_apply"
PARAM_EVENT_UI_RESET = "ui_reset"
PARAM_EVENT_MEASUREMENT_START_SNAPSHOT = "measurement_start_snapshot"

PARAM_SCOPE_SHARED = "shared"
PARAM_SCOPE_GSENSOR = "gsensor"
PARAM_SCOPES = {PARAM_SCOPE_SHARED, PARAM_SCOPE_GSENSOR}


def param_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "float"
    if isinstance(value, str):
        return "string"
    return "json"


def param_value_field(value: Any) -> tuple[str, Any]:
    value_type = param_value_type(value)
    if value_type == "bool":
        return "value_bool", value
    if value_type == "float":
        return "value_float", float(value)
    if value_type == "string":
        return "value_string", value
    return "value_json", json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class GsensorParamRecord:
    param_key: str
    value: Any
    scope: str
    source: str
    event: str
    version: int = 1
    run_id: str = "default"
    seq: int | None = None
    changed: bool = True

    def __post_init__(self) -> None:
        if not self.param_key:
            raise ValueError("param_key is required.")
        if self.scope not in PARAM_SCOPES:
            raise ValueError(f"scope must be one of {sorted(PARAM_SCOPES)}.")
        if not self.source:
            raise ValueError("source is required.")
        if not self.event:
            raise ValueError("event is required.")
        if not self.run_id:
            raise ValueError("run_id is required.")

    def tags(self) -> dict[str, str]:
        return {
            "service": GSENSOR_SERVICE_TAG,
            "run_id": self.run_id,
            "source": self.source,
            "event": self.event,
            "scope": self.scope,
            "param_key": self.param_key,
            "value_type": param_value_type(self.value),
        }

    def fields(self) -> dict[str, Any]:
        field_name, field_value = param_value_field(self.value)
        fields: dict[str, Any] = {
            field_name: field_value,
            "version": int(self.version),
            "changed": bool(self.changed),
        }
        if self.seq is not None:
            fields["seq"] = int(self.seq)
        return fields


def iter_gsensor_param_records(
    shared_params: Mapping[str, Any],
    gsensor_params: Mapping[str, Any],
    *,
    source: str,
    event: str,
    version: int = 1,
    run_id: str = "default",
    seq: int | None = None,
    changed_keys: Iterable[str] | None = None,
) -> Iterable[GsensorParamRecord]:
    changed_key_set = set(changed_keys) if changed_keys is not None else None
    for scope, params in (
        (PARAM_SCOPE_SHARED, shared_params),
        (PARAM_SCOPE_GSENSOR, gsensor_params),
    ):
        for key, value in params.items():
            yield GsensorParamRecord(
                param_key=str(key),
                value=value,
                scope=scope,
                source=source,
                event=event,
                version=version,
                run_id=run_id,
                seq=seq,
                changed=changed_key_set is None or str(key) in changed_key_set,
            )


def build_gsensor_param_point(
    record: GsensorParamRecord,
    *,
    timestamp: datetime | None = None,
) -> Any:
    return build_tagged_point(
        record.fields(),
        tags=record.tags(),
        measurement=GSENSOR_PARAMS_MEASUREMENT,
        timestamp=timestamp,
    )


def write_gsensor_param_records(
    writer: InfluxWriter,
    records: Iterable[GsensorParamRecord],
    *,
    timestamp: datetime | None = None,
) -> None:
    for record in records:
        writer.write_tagged_fields(
            record.fields(),
            tags=record.tags(),
            measurement=GSENSOR_PARAMS_MEASUREMENT,
            timestamp=timestamp,
        )


__all__ = [
    "GSENSOR_PARAMS_MEASUREMENT",
    "GSENSOR_SERVICE_TAG",
    "GsensorParamRecord",
    "PARAM_EVENT_CENTRAL_UPDATE",
    "PARAM_EVENT_MEASUREMENT_START_SNAPSHOT",
    "PARAM_EVENT_STARTUP_SNAPSHOT",
    "PARAM_EVENT_UI_APPLY",
    "PARAM_EVENT_UI_RESET",
    "PARAM_SCOPE_GSENSOR",
    "PARAM_SCOPE_SHARED",
    "PARAM_SOURCE_CENTRAL",
    "PARAM_SOURCE_DEFAULT",
    "PARAM_SOURCE_RUNTIME",
    "PARAM_SOURCE_UI",
    "build_gsensor_param_point",
    "iter_gsensor_param_records",
    "param_value_field",
    "param_value_type",
    "write_gsensor_param_records",
]
