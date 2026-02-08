import os
import threading
import time

from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue
from crystallization_mpc.infra.rabbitmq.publisher import publish
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for, route
from crystallization_mpc.messaging.schema import build_envelope
from crystallization_mpc.messaging.idgen import next_seq

ROLE = "controller"


def main():
    url = os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")
    exchange = os.getenv("RABBIT_EXCHANGE", EXCHANGE)
    queue_name = os.getenv("RABBIT_QUEUE", QUEUES[ROLE])
    include_broadcast = os.getenv("RABBIT_INCLUDE_BROADCAST", "true").lower() in ("1", "true", "yes")
    send_interval = float(os.getenv("SEND_INTERVAL_SEC", "5"))

    binding_keys = bindings_for(ROLE, include_broadcast=include_broadcast)

    def on_message(msg: dict):
        print(f"[{ROLE} recv] {msg}")

    t = threading.Thread(
        target=start_consumer,
        args=(url, exchange, queue_name, binding_keys, on_message),
        daemon=True,
    )
    t.start()

    conn, ch = connect(url)
    declare_exchange(ch, exchange)
    declare_queue(ch, queue_name, binding_keys, exchange)

    while True:
        seq = next_seq()
        for dst in ("central", "gsensor"):
            env = build_envelope(
                src=ROLE,
                dst=dst,
                msg_type="telemetry",
                name="state",
                seq=seq,
                payload={"mode": "idle", "sigma_set": 0.05},
            )
            publish(ch, exchange, route(ROLE, dst), env, persistent=False)
        time.sleep(send_interval)


if __name__ == "__main__":
    main()
