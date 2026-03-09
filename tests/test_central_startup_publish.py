import os
import time
from uuid import uuid4

import pytest

from crystallization_mpc.apps.central.ui.app import CentralApp
from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue
from crystallization_mpc.messaging.codecs import decode_json
from crystallization_mpc.messaging.routing import EXCHANGE, bindings_for


def _require_pika():
    try:
        import pika  # noqa: F401
    except Exception:
        reason = "pika not installed"
        print(f"[skip] {reason}")
        pytest.skip(reason)


def _rabbit_url() -> str:
    return os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")


def _connect_or_skip(url: str):
    try:
        return connect(url)
    except Exception as exc:
        reason = f"RabbitMQ not available: {exc}"
        print(f"[skip] {reason}")
        pytest.skip(reason)


def _setup_queue(role: str, exchange: str, url: str):
    conn, ch = _connect_or_skip(url)
    declare_exchange(ch, exchange)
    queue_name = f"test.{role}.{uuid4().hex}"
    binding_keys = bindings_for(role, include_broadcast=False)
    declare_queue(ch, queue_name, binding_keys, exchange)
    return conn, ch, queue_name


def _drain_message(ch, queue_name: str, timeout_sec: float = 5.0):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        method, properties, body = ch.basic_get(queue=queue_name, auto_ack=False)
        if method:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return decode_json(body)
        time.sleep(0.1)
    return None


def test_central_startup_publishes_params():
    _require_pika()
    url = _rabbit_url()
    exchange = EXCHANGE

    conn_g, ch_g, q_g = _setup_queue("gsensor", exchange, url)
    conn_c, ch_c, q_c = _setup_queue("controller", exchange, url)

    app = CentralApp(
        url=url,
        exchange=exchange,
        queue_name=f"test.central.{uuid4().hex}",
        include_broadcast=False,
    )
    app.connect()
    shared, gsensor, controller, version, derived = app.load_and_publish(
        params_path="params_default.yaml",
    )

    msg_g = _drain_message(ch_g, q_g)
    msg_c = _drain_message(ch_c, q_c)

    assert msg_g is not None, "gsensor did not receive params"
    assert msg_c is not None, "controller did not receive params"

    print("[central sent] shared:", shared)
    print("[central sent] gsensor:", gsensor)
    print("[central sent] controller:", controller)
    print("[gsensor recv]", msg_g.get("payload", {}))
    print("[controller recv]", msg_c.get("payload", {}))

    payload_g = msg_g.get("payload", {})
    payload_c = msg_c.get("payload", {})

    assert payload_g.get("version") == version
    assert payload_c.get("version") == version

    params_g = payload_g.get("params", {})
    params_c = payload_c.get("params", {})

    for key, value in {**shared, **gsensor}.items():
        assert params_g.get(key) == value

    for key, value in controller.items():
        assert params_c.get(key) == value

    if app._conn is not None:
        app._conn.close()
    ch_g.close()
    conn_g.close()
    ch_c.close()
    conn_c.close()
