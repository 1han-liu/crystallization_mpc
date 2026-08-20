"""Environment-backed configuration for the Controller service."""

from __future__ import annotations

import os
from dataclasses import dataclass

from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc


@dataclass(frozen=True)
class ControllerSettings:
    rabbit_url: str
    rabbit_exchange: str
    rabbit_queue: str
    adapter_spec: str | None
    opcua_enabled: bool
    opcua_endpoint: str | None
    influx_enabled: bool
    influx_url: str
    influx_org: str
    influx_bucket: str
    experiment_root: str | None = None
    influx_token: str | None = None
    opcua_namespace_index: int = 2
    opcua_t_j_set_node: str = "iCache.Temperature1"
    opcua_t_node: str = "Local.iControl.Experiment1.Trends.Tr.Value"
    opcua_t_j_node: str = "Local.iControl.Experiment1.Trends.Tj.Value"
    opcua_c_node: str = "Local.iControl.Experiment1.Trends.load.Value"
    opcua_count_middle_node: str = "Local.iControl.Experiment1.Trends.count.Value"
    opcua_timeout_s: float = 5.0
    opcua_jacket_min_K: float = 253.15
    opcua_jacket_max_K: float = 453.15
    opcua_verify_write: bool = True
    opcua_verify_tolerance_K: float = 0.05

    @classmethod
    def from_env(cls) -> "ControllerSettings":
        adapter_spec = os.getenv("CONTROLLER_ADAPTER", "").strip() or None
        opcua_endpoint = (
            os.getenv(
                "CONTROLLER_OPCUA_ENDPOINT",
                "opc.tcp://host.docker.internal:62552",
            ).strip()
            or None
        )
        return cls(
            rabbit_url=os.getenv(
                "RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F"
            ),
            rabbit_exchange=os.getenv("RABBIT_EXCHANGE", EXCHANGE),
            rabbit_queue=os.getenv("RABBIT_QUEUE", QUEUES["controller"]),
            adapter_spec=adapter_spec,
            opcua_enabled=_env_bool("CONTROLLER_OPCUA_ENABLED"),
            opcua_endpoint=opcua_endpoint,
            influx_enabled=_env_bool("CONTROLLER_INFLUX_ENABLED"),
            influx_url=(
                os.getenv("CONTROLLER_INFLUX_URL", "").strip()
                or os.getenv("INFLUX_URL", "http://influxdb:8086").strip()
            ),
            influx_org=(
                os.getenv("CONTROLLER_INFLUX_ORG", "").strip()
                or os.getenv("INFLUX_ORG", "lab").strip()
            ),
            influx_bucket=(
                os.getenv("CONTROLLER_INFLUX_BUCKET", "").strip()
                or os.getenv("INFLUX_BUCKET", "process").strip()
            ),
            experiment_root=os.getenv("EXPERIMENT_ROOT", "").strip() or None,
            influx_token=(
                os.getenv("CONTROLLER_INFLUX_TOKEN", "").strip()
                or os.getenv("INFLUX_TOKEN", "").strip()
                or None
            ),
            opcua_namespace_index=_env_int("CONTROLLER_OPCUA_NAMESPACE_INDEX", 2),
            opcua_t_j_set_node=os.getenv(
                "CONTROLLER_OPCUA_T_J_SET_NODE", "iCache.Temperature1"
            ),
            opcua_t_node=os.getenv(
                "CONTROLLER_OPCUA_T_NODE",
                "Local.iControl.Experiment1.Trends.Tr.Value",
            ),
            opcua_t_j_node=os.getenv(
                "CONTROLLER_OPCUA_T_J_NODE",
                "Local.iControl.Experiment1.Trends.Tj.Value",
            ),
            opcua_c_node=os.getenv(
                "CONTROLLER_OPCUA_C_NODE",
                "Local.iControl.Experiment1.Trends.load.Value",
            ),
            opcua_count_middle_node=os.getenv(
                "CONTROLLER_OPCUA_COUNT_MIDDLE_NODE",
                "Local.iControl.Experiment1.Trends.count.Value",
            ),
            opcua_timeout_s=_env_float("CONTROLLER_OPCUA_TIMEOUT_S", 5.0),
            opcua_jacket_min_K=_env_float(
                "CONTROLLER_OPCUA_JACKET_MIN_K", 253.15
            ),
            opcua_jacket_max_K=_env_float(
                "CONTROLLER_OPCUA_JACKET_MAX_K", 453.15
            ),
            opcua_verify_write=_env_bool("CONTROLLER_OPCUA_VERIFY_WRITE", True),
            opcua_verify_tolerance_K=_env_float(
                "CONTROLLER_OPCUA_VERIFY_TOLERANCE_K", 0.05
            ),
        )

    def integration_status(self) -> dict[str, dict[str, object]]:
        return {
            "opcua": {
                "enabled": self.opcua_enabled,
                "configured": bool(self.opcua_endpoint),
                "endpoint": self.opcua_endpoint,
                "connected": False,
            },
            "influxdb": {
                "enabled": self.influx_enabled,
                "configured": bool(
                    self.influx_url
                    and self.influx_token
                    and self.influx_org
                    and self.influx_bucket
                ),
                "url": self.influx_url,
                "org": self.influx_org,
                "bucket": self.influx_bucket,
                "connected": False,
            },
        }


__all__ = ["ControllerSettings"]
