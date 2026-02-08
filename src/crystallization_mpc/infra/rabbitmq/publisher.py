from typing import Any, Dict

try:
    import pika
except Exception:
    pika = None

from crystallization_mpc.messaging.codecs import encode_json


def publish(channel, exchange: str, routing_key: str, payload: Dict[str, Any], persistent: bool = True):
    body = encode_json(payload)
    props = pika.BasicProperties(delivery_mode=2) if persistent and pika is not None else None
    channel.basic_publish(exchange=exchange, routing_key=routing_key, body=body, properties=props)
