"""Serial, durable setpoint execution and confirmed RabbitMQ results."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from crystallization_mpc.apps.controller.process import ProcessSafetyError
from crystallization_mpc.infra.rabbitmq.connection import connect

from .commands import COMMAND_ROUTE, RESULT_ROUTE, WriteCommand, result_envelope
from .config import WriterSettings
from .device import SetpointDevice
from .journal import CommandJournal

log = logging.getLogger(__name__)


class CommandExecutor:
    """Calls the device once at most; database and device stay on one thread."""

    def __init__(
        self,
        settings: WriterSettings,
        *,
        journal: CommandJournal | None = None,
        device=None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.journal = journal or CommandJournal(settings.journal_path)
        self.device = device or SetpointDevice(settings)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.last_result: dict[str, Any] | None = None
        if self.journal.recovered_count:
            log.warning("Recovered %d interrupted commands as unknown; no automatic replay", self.journal.recovered_count)

    def execute(self, message: Mapping[str, Any]) -> dict[str, Any]:
        try:
            command = WriteCommand.from_envelope(message)
        except (ValueError, TypeError, ProcessSafetyError) as exc:
            raw = message.get("payload", {})
            raw = raw if isinstance(raw, Mapping) else {}
            result = {
                "command_id": raw.get("command_id") if isinstance(raw.get("command_id"), str) else None,
                "run_id": raw.get("run_id") if isinstance(raw.get("run_id"), str) else None,
                "run_started_at": raw.get("run_started_at") if isinstance(raw.get("run_started_at"), str) else None,
                "tick_seq": raw.get("tick_seq") if type(raw.get("tick_seq")) is int else 0,
                "status": "failed",
                "error": f"Invalid command: {exc}",
                "write_result": None,
            }
            self.journal.audit_rejection(result)
            return self._record_log(result)

        current = self.now()
        rejection = None
        if command.expires_at <= current:
            rejection = "Command expired before execution."
        elif command.run_started_at > current + timedelta(seconds=5):
            rejection = "Experiment start timestamp is in the future."
        elif not self.settings.process.jacket_min_K <= command.setpoint_K <= self.settings.process.jacket_max_K:
            rejection = "T_j_set is outside the configured safe jacket-temperature range."

        # Reserve first even for a rejection: duplicates must retain their outcome.
        previous = self.journal.reserve(command, rejection=rejection)
        if previous is not None:
            return self._record_log(previous)

        try:
            self.device.connect()
        except Exception as exc:
            result = command.result("failed", f"OPC UA connection failed before write: {self._error_text(exc)}")
        else:
            if command.expires_at <= self.now():
                result = command.result("failed", "Command expired while connecting; no write attempted.")
            else:
                try:
                    write_result = self.device.write_jacket_setpoint(command.setpoint_K)
                except ProcessSafetyError as exc:
                    result = command.result("failed", f"OPC UA write/readback validation failed: {self._error_text(exc)}")
                except Exception as exc:
                    if _explicit_opcua_rejection(exc):
                        result = command.result("failed", f"OPC UA rejected write/readback: {self._error_text(exc)}")
                    else:
                        result = command.result(
                            "unknown",
                            f"Connection or execution failed after write was attempted; outcome unknown: {self._error_text(exc)}",
                        )
                else:
                    result = command.result("success", write_result=write_result.to_dict())
        # If this commit fails we must not ACK. The durable executing state will
        # become unknown on restart instead of repeating a potentially applied write.
        self.journal.finish(command.command_id, result)
        return self._record_log(result)

    def _error_text(self, error: Exception) -> str:
        text = str(error)
        if self.settings.password:
            text = text.replace(self.settings.password, "[redacted]")
        # OPC UA libraries may include URLs in exceptions. Results are persisted,
        # published, and displayed in Controller status, so remove URL userinfo.
        text = re.sub(r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1[redacted]@", text, flags=re.IGNORECASE)
        return f"{type(error).__name__}: {text}"

    def _record_log(self, result: dict[str, Any]) -> dict[str, Any]:
        self.last_result = result
        log.info("setpoint_result %s", json.dumps(result, allow_nan=False))
        return result

    def close(self) -> None:
        self.device.disconnect()
        self.journal.close()


def _explicit_opcua_rejection(error: Exception) -> bool:
    try:
        from asyncua.ua.uaerrors import UaStatusCodeError
    except ImportError:
        return False
    if not isinstance(error, UaStatusCodeError):
        return False
    # Protocol rejections such as access denied are definite failures. Session
    # and timeout statuses can occur after applying Write, so remain uncertain.
    return type(error).__name__ in {
        "BadUserAccessDenied", "BadNotWritable", "BadTypeMismatch",
        "BadOutOfRange", "BadNodeIdUnknown", "BadNodeIdInvalid",
        "BadWriteNotSupported", "BadAttributeIdInvalid", "BadIndexRangeInvalid",
        "BadIndexRangeNoData", "BadDataEncodingInvalid", "BadDataEncodingUnsupported",
    }


class WriterService:
    """All Pika and SQLite calls run on the caller's thread.

    The only cross-thread operation is setting a stop Event. A lost result or
    publisher confirmation causes broker redelivery and journal replay, never
    another device write.
    """

    def __init__(self, settings: WriterSettings, *, executor=None, connector=connect) -> None:
        self.settings = settings
        self.executor = executor or CommandExecutor(settings)
        self.connector = connector
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def deliver(self, channel, method, properties, body: bytes) -> None:
        import pika

        try:
            message = json.loads(body)
            if not isinstance(message, dict):
                raise ValueError("Envelope must be a JSON object.")
        except (ValueError, UnicodeDecodeError):
            message = {}
        result = self.executor.execute(message)
        envelope = result_envelope(result)
        confirmed = channel.basic_publish(
            exchange=self.settings.rabbit_exchange,
            routing_key=RESULT_ROUTE,
            body=json.dumps(envelope, allow_nan=False).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                correlation_id=result.get("command_id"),
            ),
            mandatory=True,
        )
        if confirmed is False:
            raise RuntimeError("RabbitMQ did not confirm the process write result.")
        channel.basic_ack(delivery_tag=method.delivery_tag)

    def run(self) -> None:
        from .telemetry import WriterAuditRelay
        relay = WriterAuditRelay(self.executor.journal.path)
        relay.start()
        try:
            while not self.stop_event.is_set():
                connection = None
                try:
                    connection, channel = self.connector(self.settings.rabbit_url)
                    channel.exchange_declare(exchange=self.settings.rabbit_exchange, exchange_type="topic", durable=True)
                    channel.queue_declare(queue=self.settings.rabbit_queue, durable=True)
                    channel.queue_bind(
                        queue=self.settings.rabbit_queue,
                        exchange=self.settings.rabbit_exchange,
                        routing_key=COMMAND_ROUTE,
                    )
                    channel.basic_qos(prefetch_count=1)
                    channel.confirm_delivery()
                    log.info("Writer consuming queue=%s; durable journal ready", self.settings.rabbit_queue)
                    for method, properties, body in channel.consume(
                        self.settings.rabbit_queue, inactivity_timeout=0.5, auto_ack=False, exclusive=True
                    ):
                        if self.stop_event.is_set():
                            break
                        if method is not None:
                            self.deliver(channel, method, properties, body)
                    channel.cancel()
                except Exception as exc:
                    # Closing the connection requeues an unacknowledged command.
                    # Its persisted result will be republished after reconnect.
                    # Pika exception messages may include the authenticated URL.
                    log.error("Writer transport interrupted (%s)", type(exc).__name__)
                finally:
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass
                self.stop_event.wait(1.0)
        finally:
            relay.close()
            self.executor.close()
