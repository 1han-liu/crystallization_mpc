import json
try:
    import pika
except Exception:
    pika = None

class RabbitBus:
    def __init__(self, url: str):
        self.enabled = pika is not None
        if self.enabled:
            self.params = pika.URLParameters(url)
            self.conn = pika.BlockingConnection(self.params)
            self.ch = self.conn.channel()
            self.ch.exchange_declare(exchange="control.topic", exchange_type="topic", durable=True)
        else:
            print("[RabbitBus] pika not installed; bus disabled.")

    def publish_setpoint(self, u: float, T_set: float):
        if not self.enabled:
            print(f"[RabbitBus disabled] u={u:.5f}, T_set={T_set:.3f}")
            return
        payload = {"dT_dt": u, "T_set": T_set}
        self.ch.basic_publish(
            exchange="control.topic",
            routing_key="control.crystallizer.setpoint",
            body=json.dumps(payload).encode(),
            properties=pika.BasicProperties(delivery_mode=2),
        )

