from __future__ import annotations

import os
from typing import Dict, Optional, Set, Tuple

from crystallization_mpc.apps.central.params import apply_derived_params, load_params
from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue
from crystallization_mpc.infra.rabbitmq.publisher import publish
from crystallization_mpc.messaging.idgen import next_seq
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for, route
from crystallization_mpc.messaging.schema import build_envelope

ROLE = "central"


class CentralApp:
    def __init__(
        self,
        url: Optional[str] = None,
        exchange: Optional[str] = None,
        queue_name: Optional[str] = None,
        include_broadcast: bool = True,
    ) -> None:
        self.url = url or os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")
        self.exchange = exchange or os.getenv("RABBIT_EXCHANGE", EXCHANGE)
        self.queue_name = queue_name or os.getenv("RABBIT_QUEUE", QUEUES[ROLE])
        self.include_broadcast = include_broadcast
        self._conn = None
        self._ch = None

    def connect(self) -> None:
        binding_keys = bindings_for(ROLE, include_broadcast=self.include_broadcast)
        self._conn, self._ch = connect(self.url)
        declare_exchange(self._ch, self.exchange)
        declare_queue(self._ch, self.queue_name, binding_keys, self.exchange)

    def _require_channel(self):
        if self._ch is None:
            raise RuntimeError("CentralApp is not connected. Call connect() first.")
        return self._ch

    def publish_params(
        self,
        shared: Dict[str, object],
        gsensor: Dict[str, object],
        controller: Dict[str, object],
        version: int,
        controller_allowlist: Optional[Set[str]] = None,
    ) -> None:
        ch = self._require_channel()
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
            publish(ch, self.exchange, route(ROLE, "gsensor"), env, persistent=True)

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
            publish(ch, self.exchange, route(ROLE, "controller"), env, persistent=True)

    def load_and_publish(
        self,
        params_path: Optional[str] = None,
        controller_allowlist: Optional[Set[str]] = None,
        target: Optional[str] = None, # ui回调位置接口
    ) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, object], int, Dict[str, object]]:
        path = params_path or os.getenv("PARAMS_FILE", "params_default.yaml")
        shared, gsensor, controller, version = load_params(path)
        shared, controller, derived = apply_derived_params(shared, controller, target=target)
        self.publish_params(shared, gsensor, controller, version)
        return shared, gsensor, controller, version, derived


__all__ = ["CentralApp"]
