import uuid
from typing import Any, Dict

from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange
from crystallization_mpc.messaging.codecs import encode_json, decode_json


class RpcClient:
    def __init__(self, url: str, exchange: str):
        self.conn, self.ch = connect(url)
        self.exchange = exchange
        declare_exchange(self.ch, exchange)
        result = self.ch.queue_declare(queue="", exclusive=True)
        self.callback_queue = result.method.queue
        self.response = None
        self.corr_id = None
        self.ch.basic_consume(
            queue=self.callback_queue,
            on_message_callback=self._on_response,
            auto_ack=True,
        )

    def _on_response(self, ch, method, props, body):
        if self.corr_id == props.correlation_id:
            self.response = decode_json(body)

    def call(self, routing_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.response = None
        self.corr_id = str(uuid.uuid4())
        props = type("P", (), {"reply_to": self.callback_queue, "correlation_id": self.corr_id})()
        self.ch.basic_publish(
            exchange=self.exchange,
            routing_key=routing_key,
            body=encode_json(payload),
            properties=props,
        )
        while self.response is None:
            self.conn.process_data_events(time_limit=1)
        return self.response


class RpcServer:
    def __init__(self, url: str, exchange: str, queue_name: str, routing_key: str):
        self.conn, self.ch = connect(url)
        self.exchange = exchange
        declare_exchange(self.ch, exchange)
        self.ch.queue_declare(queue=queue_name, durable=True)
        self.ch.queue_bind(exchange=exchange, queue=queue_name, routing_key=routing_key)
        self.ch.basic_qos(prefetch_count=1)

    def start(self, handler):
        def _on_request(ch, method, props, body):
            req = decode_json(body)
            resp = handler(req)
            props_resp = type("P", (), {"correlation_id": props.correlation_id})()
            ch.basic_publish(
                exchange="",
                routing_key=props.reply_to,
                body=encode_json(resp),
                properties=props_resp,
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)

        self.ch.basic_consume(queue=self.ch.queue, on_message_callback=_on_request)
        self.ch.start_consuming()
