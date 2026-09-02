from typing import Callable, Iterable, Optional

from crystallization_mpc.messaging.codecs import decode_json
from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue


def start_consumer(
    url: str,
    exchange: str,
    queue_name: str,
    binding_keys: Iterable[str],
    on_message: Callable[[dict], None],
    on_ready: Optional[Callable[[], None]] = None,
) -> None:
    conn, ch = connect(url)
    declare_exchange(ch, exchange)
    declare_queue(ch, queue_name, binding_keys, exchange)
    ch.basic_qos(prefetch_count=1)

    def _callback(ch, method, properties, body):
        try:
            msg = decode_json(body)
        except Exception:
            msg = {"raw": body.decode(errors="ignore")}
        on_message(msg)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    ch.basic_consume(queue=queue_name, on_message_callback=_callback, auto_ack=False)
    if on_ready is not None:
        on_ready()
    ch.start_consuming()
