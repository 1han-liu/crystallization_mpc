"""Environment-backed Controller configuration; equipment connections live in the writer."""

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
    process_enabled: bool
    influx_enabled: bool
    influx_url: str
    influx_org: str
    influx_bucket: str
    process_write_enabled: bool = False
    experiment_root: str | None = None
    influx_token: str | None = None
    process_device_id: str = "crystallizer"
    process_measurement_name: str = "crystallizer_raw"
    process_telemetry_queue: str = "process.telemetry"
    process_result_queue: str = "process.results"
    process_command_ttl_s: float = 5.0
    process_write_timeout_s: float = 5.0
    # Expected telemetry identities only; no endpoint, login or certificates.
    process_namespace_index: int = 2
    process_t_j_set_node: str = "iCache.Temperature1"
    process_t_node: str = "Local.iControl.Experiment1.Trends.Tr.Value"
    process_t_j_node: str = "Local.iControl.Experiment1.Trends.Tj.Value"
    process_c_node: str = "Local.iControl.Experiment1.Trends.load.Value"
    process_count_middle_node: str = "Local.iControl.Experiment1.Trends.count.Value"

    @classmethod
    def from_env(cls) -> "ControllerSettings":
        # Never silently reactivate the removed direct OPC UA path from an old .env.
        if _env_bool("CONTROLLER_OPCUA_ENABLED") or _env_bool("CONTROLLER_OPCUA_WRITE_ENABLED"):
            raise ValueError(
                "CONTROLLER_OPCUA_* direct I/O was removed. Migrate to "
                "CONTROLLER_PROCESS_ENABLED/CONTROLLER_PROCESS_WRITE_ENABLED and "
                "the process-io services; equipment configuration belongs in OPCUA_*."
            )
        return cls(
            rabbit_url=os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F"),
            rabbit_exchange=os.getenv("RABBIT_EXCHANGE", EXCHANGE),
            rabbit_queue=os.getenv("RABBIT_QUEUE", QUEUES["controller"]),
            adapter_spec=os.getenv("CONTROLLER_ADAPTER", "").strip() or "crystallization_mpc.apps.controller.algorithm.integration:CrystallizationControllerAdapter",
            process_enabled=_env_bool("CONTROLLER_PROCESS_ENABLED"),
            process_write_enabled=_env_bool("CONTROLLER_PROCESS_WRITE_ENABLED"),
            influx_enabled=_env_bool("CONTROLLER_INFLUX_ENABLED"),
            influx_url=(os.getenv("CONTROLLER_INFLUX_URL", "").strip()
                        or os.getenv("INFLUX_URL", "http://influxdb:8086").strip()),
            influx_org=(os.getenv("CONTROLLER_INFLUX_ORG", "").strip()
                        or os.getenv("INFLUX_ORG", "lab").strip()),
            influx_bucket=(os.getenv("CONTROLLER_INFLUX_BUCKET", "").strip()
                           or os.getenv("INFLUX_BUCKET", "process").strip()),
            experiment_root=os.getenv("EXPERIMENT_ROOT", "").strip() or None,
            influx_token=(os.getenv("CONTROLLER_INFLUX_TOKEN", "").strip()
                          or os.getenv("INFLUX_TOKEN", "").strip() or None),
            process_device_id=os.getenv("PROCESS_DEVICE_ID", "crystallizer"),
            process_measurement_name=os.getenv("PROCESS_MEASUREMENT_NAME", "crystallizer_raw"),
            process_telemetry_queue=os.getenv("PROCESS_TELEMETRY_QUEUE", "process.telemetry"),
            process_result_queue=os.getenv("PROCESS_RESULT_QUEUE", "process.results"),
            process_command_ttl_s=_env_float("PROCESS_COMMAND_TTL_S", 5),
            process_write_timeout_s=_env_float("PROCESS_WRITE_TIMEOUT_S", 5),
            process_namespace_index=_env_int("PROCESS_NAMESPACE_INDEX", 2),
            process_t_j_set_node=os.getenv("PROCESS_T_J_SET_NODE", "iCache.Temperature1"),
            process_t_node=os.getenv("PROCESS_T_NODE", "Local.iControl.Experiment1.Trends.Tr.Value"),
            process_t_j_node=os.getenv("PROCESS_T_J_NODE", "Local.iControl.Experiment1.Trends.Tj.Value"),
            process_c_node=os.getenv("PROCESS_C_NODE", "Local.iControl.Experiment1.Trends.load.Value"),
            process_count_middle_node=os.getenv("PROCESS_COUNT_MIDDLE_NODE", "Local.iControl.Experiment1.Trends.count.Value"),
        )

    def integration_status(self) -> dict[str, dict[str, object]]:
        return {
            "process": {
                "enabled": self.process_enabled,
                "write_enabled": self.process_write_enabled,
                "shadow_mode": self.process_enabled and not self.process_write_enabled,
                "transport": "rabbitmq",
                "configured": bool(self.rabbit_url and self.rabbit_exchange),
                "device_id": self.process_device_id,
                "connected": False,
            },
            "influxdb": {
                "enabled": self.influx_enabled,
                "configured": bool(self.influx_url and self.influx_token
                                   and self.influx_org and self.influx_bucket),
                "url": self.influx_url,
                "org": self.influx_org,
                "bucket": self.influx_bucket,
                "connected": False,
            },
        }


__all__ = ["ControllerSettings"]
