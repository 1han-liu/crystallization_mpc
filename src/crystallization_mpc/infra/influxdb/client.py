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


def load_influx_settings(*, prefix: str | None = None) -> InfluxSettings:
    env_file_values = _read_dotenv_file()

    def value(name: str, default: str | None = None) -> str | None:
        keys = ([f"{prefix}_INFLUX_{name}"] if prefix else []) + [f"INFLUX_{name}"]
        return (next((os.environ[key].strip() for key in keys if os.getenv(key, "").strip()), None)
                or next((env_file_values[key].strip() for key in keys if env_file_values.get(key, "").strip()), None)
                or default)

    token = value("TOKEN")
    if not token:
        raise RuntimeError("INFLUX_TOKEN is required.")

    return InfluxSettings(
        url=value("URL", "http://localhost:8087"),
        token=token,
        org=value("ORG", "lab"),
        bucket=value("BUCKET", "process"),
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
