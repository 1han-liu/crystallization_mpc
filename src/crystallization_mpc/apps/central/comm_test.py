import os
import threading
import time
from typing import Dict, Tuple

from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue
from crystallization_mpc.infra.rabbitmq.publisher import publish
from crystallization_mpc.infra.rabbitmq.consumer import start_consumer
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for, route
from crystallization_mpc.messaging.schema import build_envelope
from crystallization_mpc.messaging.idgen import next_seq

ROLE = "central"


def _require_yaml():
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise RuntimeError("pyyaml is required to load params_default.yaml") from exc
    return yaml


def _list_to_dict(items) -> Dict[str, object]:
    params: Dict[str, object] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key:
            continue
        params[str(key)] = item.get("default")
    return params


def load_params(path: str) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int]:
    if not os.path.exists(path):
        print(f"[{ROLE}] params file not found: {path}")
        return {}, {}, {}, 1
    yaml = _require_yaml()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    params = data.get("params", {}) or {}
    version = int(data.get("version", 1))
    shared = _list_to_dict(params.get("shared"))
    gsensor = _list_to_dict(params.get("gsensor"))
    controller = _list_to_dict(params.get("controller"))
    return shared, gsensor, controller, version


def publish_params(ch, exchange: str, shared, gsensor, controller, version: int):
    seq = next_seq()
    if shared or gsensor:
        payload = {"version": version, "params": {**shared, **gsensor}}
        env = build_envelope(
            src=ROLE,
            dst="gsensor",
            msg_type="params",
            name="update",
            seq=seq,
            payload=payload,
        )
        publish(ch, exchange, route(ROLE, "gsensor"), env, persistent=True)
        print(f"[{ROLE}] params sent -> gsensor (keys={list(payload['params'].keys())})")
    if shared or controller:
        payload = {"version": version, "params": {**shared, **controller}}
        env = build_envelope(
            src=ROLE,
            dst="controller",
            msg_type="params",
            name="update",
            seq=seq,
            payload=payload,
        )
        publish(ch, exchange, route(ROLE, "controller"), env, persistent=True)
        print(f"[{ROLE}] params sent -> controller (keys={list(payload['params'].keys())})")


def main():
    url = os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")
    exchange = os.getenv("RABBIT_EXCHANGE", EXCHANGE)
    queue_name = os.getenv("RABBIT_QUEUE", QUEUES[ROLE])
    include_broadcast = os.getenv("RABBIT_INCLUDE_BROADCAST", "true").lower() in ("1", "true", "yes")
#    send_interval = float(os.getenv("SEND_INTERVAL_SEC", "5"))
    params_path = os.getenv("PARAMS_FILE", "params_default.yaml")
    send_params_on_start = os.getenv("SEND_PARAMS_ON_START", "true").lower() in ("1", "true", "yes")

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

    if send_params_on_start:
        shared, gsensor, controller, version = load_params(params_path)
        publish_params(ch, exchange, shared, gsensor, controller, version)

    # Keep process alive for testing without sending periodic traffic.
    while True:
        time.sleep(send_interval)


if __name__ == "__main__":
    main()
