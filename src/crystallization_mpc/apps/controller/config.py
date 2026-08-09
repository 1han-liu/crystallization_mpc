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

    @classmethod
    def from_env(cls) -> "ControllerSettings":
        adapter_spec = os.getenv("CONTROLLER_ADAPTER", "").strip() or None
        opcua_endpoint = os.getenv("CONTROLLER_OPCUA_ENDPOINT", "").strip() or None
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
            influx_url=os.getenv("CONTROLLER_INFLUX_URL", "http://influxdb:8086"),
            influx_org=os.getenv("CONTROLLER_INFLUX_ORG", "lab"),
            influx_bucket=os.getenv("CONTROLLER_INFLUX_BUCKET", "process"),
            experiment_root=os.getenv("EXPERIMENT_ROOT", "").strip() or None,
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
                    self.influx_url and self.influx_org and self.influx_bucket
                ),
                "url": self.influx_url,
                "org": self.influx_org,
                "bucket": self.influx_bucket,
                "connected": False,
            },
        }


__all__ = ["ControllerSettings"]
