"""RabbitMQ publisher for Gsensor lifecycle status messages."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol

from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.publisher import publish
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue
from crystallization_mpc.messaging.routing import bindings_for

logger = logging.getLogger(__name__)


class StatusPublisher(Protocol):
    def publish(self, routing_key: str, message: Mapping[str, Any]) -> None:
        """Publish one already-built message envelope."""


class RabbitStatusPublisher:
    """Publish persistent status messages and reconnect once after a failure."""

    def __init__(
        self,
        *,
        url: str,
        exchange: str,
        destination_queue: str,
        destination_role: str,
    ) -> None:
        self.url = url
        self.exchange = exchange
        self.destination_queue = destination_queue
        self.destination_role = destination_role
        self._connection = None
        self._channel = None

    def _is_connection_open(self) -> bool:
        return self._connection is not None and bool(
            getattr(self._connection, "is_open", False)
        )

    def _is_channel_open(self) -> bool:
        return self._channel is not None and bool(
            getattr(self._channel, "is_open", False)
        )

    def connect(self, *, force: bool = False) -> None:
        if not force and self._is_connection_open() and self._is_channel_open():
            return
        if force:
            self.close()
        self._connection, self._channel = connect(self.url)
        declare_exchange(self._channel, self.exchange)
        declare_queue(
            self._channel,
            self.destination_queue,
            bindings_for(self.destination_role),
            self.exchange,
        )

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                logger.exception("Failed to close the Gsensor publisher connection.")
        self._connection = None
        self._channel = None

    def publish(self, routing_key: str, message: Mapping[str, Any]) -> None:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if not self._is_connection_open() or not self._is_channel_open():
                    self.connect(force=True)
                publish(
                    self._channel,
                    self.exchange,
                    routing_key,
                    dict(message),
                    persistent=True,
                )
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Gsensor status publish failed on attempt %s/2.",
                    attempt + 1,
                    exc_info=True,
                )
                self.close()
        raise RuntimeError("Gsensor status publish failed after reconnect.") from last_error


__all__ = ["RabbitStatusPublisher", "StatusPublisher"]
