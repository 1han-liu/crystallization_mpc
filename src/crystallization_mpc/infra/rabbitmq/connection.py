from typing import Tuple

try:
    import pika
except Exception:
    pika = None


def connect(url: str) -> Tuple["pika.BlockingConnection", "pika.channel.Channel"]:
    if pika is None:
        raise RuntimeError("pika is not installed")
    params = pika.URLParameters(url)
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    return conn, ch
