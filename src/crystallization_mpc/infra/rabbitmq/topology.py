from typing import Iterable


def declare_exchange(channel, exchange: str):
    channel.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)


def declare_queue(channel, queue_name: str, binding_keys: Iterable[str], exchange: str):
    channel.queue_declare(queue=queue_name, durable=True)
    for key in binding_keys:
        channel.queue_bind(exchange=exchange, queue=queue_name, routing_key=key)
