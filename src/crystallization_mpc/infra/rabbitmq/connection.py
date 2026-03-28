import os
from typing import TYPE_CHECKING, Any, Tuple

try:
    import pika
except Exception:
    pika = None

if TYPE_CHECKING:
    import pika as pika_types


def _require_pika() -> Any:
    if pika is None:
        raise RuntimeError("pika is not installed")
    return pika


def _build_params(url: str) -> "pika_types.URLParameters":
    pika_module = _require_pika()
    params = pika_module.URLParameters(url)
    params.heartbeat = int(os.getenv("RABBIT_HEARTBEAT_SEC", "60"))
    params.blocked_connection_timeout = int(os.getenv("RABBIT_BLOCKED_TIMEOUT_SEC", "30"))
    params.connection_attempts = int(os.getenv("RABBIT_CONNECTION_ATTEMPTS", "3"))
    params.retry_delay = int(os.getenv("RABBIT_RETRY_DELAY_SEC", "2"))
    params.socket_timeout = int(os.getenv("RABBIT_SOCKET_TIMEOUT_SEC", "10"))
    return params

def connect(url: str) -> Tuple["pika_types.BlockingConnection", Any]:
    pika_module = _require_pika()
    params = _build_params(url)
    conn = pika_module.BlockingConnection(params)
    ch = conn.channel()
    return conn, ch
