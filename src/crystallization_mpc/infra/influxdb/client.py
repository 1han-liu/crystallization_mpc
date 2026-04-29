from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from influxdb_client import InfluxDBClient
except ImportError as exc:  # pragma: no cover - exercised at runtime
    InfluxDBClient = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@dataclass(frozen=True)
class InfluxSettings:
    url: str
    token: str
    org: str
    bucket: str


PROJECT_ROOT = Path(__file__).resolve().parents[4]
ENV_FILE = PROJECT_ROOT / ".env"


def _read_dotenv_file() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_influx_settings() -> InfluxSettings:
    env_file_values = _read_dotenv_file()
    token = os.getenv("INFLUX_TOKEN") or env_file_values.get("INFLUX_TOKEN")
    if not token:
        raise RuntimeError("INFLUX_TOKEN is required.")

    return InfluxSettings(
        url=os.getenv("INFLUX_URL") or env_file_values.get("INFLUX_URL") or "http://localhost:8087",
        token=token,
        org=os.getenv("INFLUX_ORG") or env_file_values.get("INFLUX_ORG") or "lab",
        bucket=os.getenv("INFLUX_BUCKET") or env_file_values.get("INFLUX_BUCKET") or "process",
    )


def create_influx_client(settings: InfluxSettings | None = None) -> InfluxDBClient:
    if InfluxDBClient is None:
        raise RuntimeError("influxdb-client is not installed.") from _IMPORT_ERROR

    active_settings = settings or load_influx_settings()
    return InfluxDBClient(
        url=active_settings.url,
        token=active_settings.token,
        org=active_settings.org,
    )


__all__ = ["InfluxSettings", "create_influx_client", "load_influx_settings"]
