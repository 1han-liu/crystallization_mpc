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

from .commands import (
    COMMAND_ROUTE, RESULT_ROUTE, CONFIRM_COMMAND, STATUS_COMMAND,
    RecoveryQuery, WriteCommand, result_envelope,
)
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
        if message.get("name") in {STATUS_COMMAND, CONFIRM_COMMAND}:
            return self._recover(message)
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
                "failure_kind": "validation",
                "execution_complete": True,
                "manual_confirmation_required": False,
                "recoverable": False,
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
            return self._record_log(self.journal._recovery_result(previous))

        try:
            self.device.connect()
        except Exception as exc:
            result = command.result("failed", f"OPC UA connection failed before write: {self._error_text(exc)}")
            result["failure_kind"] = "validation" if _definite_failure(exc) else "communication"
        else:
            if command.expires_at <= self.now():
                result = command.result("failed", "Command expired while connecting; no write attempted.")
                result["failure_kind"] = "communication"
            else:
                try:
                    write_result = self.device.write_jacket_setpoint(command.setpoint_K)
                except ProcessSafetyError as exc:
                    result = command.result("failed", f"OPC UA write/readback validation failed: {self._error_text(exc)}")
                    result["failure_kind"] = "validation"
                except Exception as exc:
                    if _definite_failure(exc):
                        result = command.result("failed", f"OPC UA rejected write/readback: {self._error_text(exc)}")
                        result["failure_kind"] = "validation"
                    else:
                        result = command.result(
                            "unknown",
                            f"Connection or execution failed after write was attempted; outcome unknown: {self._error_text(exc)}",
                        )
                        result["failure_kind"] = "communication"
                else:
                    result = command.result("success", write_result=write_result.to_dict())
        # If this commit fails we must not ACK. The durable executing state will
        # become unknown on restart instead of repeating a potentially applied write.
        result = self.journal._recovery_result(result)
        self.journal.finish(command.command_id, result)
        return self._record_log(result)

    def _recover(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """Serialize recovery with writes, without connecting to or writing the device."""
        try:
            query = RecoveryQuery.from_envelope(message)
            if not self.settings.process.jacket_min_K <= query.command.setpoint_K <= self.settings.process.jacket_max_K:
                raise ProcessSafetyError("Original T_j_set is outside the configured safe range.")
            if query.command.run_started_at > self.now() + timedelta(seconds=5):
                raise ProcessSafetyError("Original experiment start timestamp is in the future.")
        except (ValueError, TypeError, ProcessSafetyError) as exc:
            raw = message.get("payload", {})
            raw = raw if isinstance(raw, Mapping) else {}
            result = {key: raw.get(key) for key in ("command_id", "run_id", "run_started_at", "query_id")}
            result.update(tick_seq=raw.get("tick_seq") if type(raw.get("tick_seq")) is int else 0,
                          status="failed", error=f"Invalid recovery command: {exc}", write_result=None,
                          failure_kind="validation", execution_complete=True,
                          manual_confirmation_required=False, recoverable=False)
            if type(raw.get("recovery_generation")) is int and raw["recovery_generation"] >= 0:
                result["recovery_generation"] = raw["recovery_generation"]
            self.journal.audit_rejection(result)
            return self._record_log(result)

        if query.confirm:
            result = self.journal.confirm_unknown(query.command, {
                "query_id": query.query_id, "operator_confirmed": True,
                "confirmed_at": self.now().isoformat(),
                # Controller must read a post-confirmation complete process
                # baseline before it is permitted to calculate or write again.
                "requires_fresh_process_baseline": True,
            })
        else:
            result = self.journal.recovery_status(query.command)
        result = query.result(result)
        self.journal.audit_recovery(result, query.query_id)
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


def _definite_failure(error: Exception) -> bool:
    """A wrapped bad value or access/configuration rejection is not a timeout."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ProcessSafetyError) or _explicit_opcua_rejection(error):
            return True
        if type(error).__name__ in {
            "BadIdentityTokenInvalid", "BadIdentityTokenRejected", "BadSecurityPolicyRejected",
            "BadSecurityModeRejected", "BadCertificateInvalid", "BadCertificateUntrusted",
            "BadUserSignatureInvalid", "BadApplicationSignatureInvalid",
        }:
            return True
        error = error.__cause__ or error.__context__
    return False


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
        recovery = message.get("name") in {STATUS_COMMAND, CONFIRM_COMMAND}
        envelope = result_envelope(result, recovery=recovery)
        confirmed = channel.basic_publish(
            exchange=self.settings.rabbit_exchange,
            routing_key=RESULT_ROUTE,
            body=json.dumps(envelope, allow_nan=False).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                correlation_id=result.get("query_id") if recovery else result.get("command_id"),
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
