"""RabbitMQ process I/O, independent of the Controller business-message thread.

Telegraf emits one metric per OPC UA node. Only a complete collection timestamp
is exposed as a ProcessState; timestamps are never rounded or joined by arrival.
"""

from __future__ import annotations

import json
import copy
import math
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Callable, Mapping

import pika

from crystallization_mpc.messaging.schema import build_envelope
from crystallization_mpc.infra.influxdb.process_events import write_process_event

from .process import (
    CELSIUS_TO_KELVIN,
    OpcUaNodeConfig,
    ProcessAdapter,
    ProcessConnectionError,
    ProcessDataTimeoutPending,
    ProcessMeasurementPending,
    ProcessTransportPending,
    ProcessWritePending,
    ProcessSafetyError,
    ProcessState,
    ProcessWriteResult,
)

FIELDS = ("T", "T_j", "c", "count_middle", "T_j_set")
TELEMETRY_ROUTE = "telegraf.to.process"
COMMAND_ROUTE = "controller.to.opcua_writer"
RESULT_ROUTE = "opcua_writer.to.process"
# gopcua's pinned-version StatusCode.String() is verbose; older fixtures used
# the short representation. Only these exact strings (or integer zero) are Good.
GOOD_QUALITY_STRINGS = frozenset({
    "OK (0x0)",
    "The operation succeeded. StatusGood (0x0)",
})
# Deliberately narrow: configuration, node-identity and arbitrary bad-quality
# errors are still hard faults. These status codes only reject a communication
# affected round and wait for a fresh complete collection after the boundary.
COMMUNICATION_STATUS_CODES = frozenset({
    0x80050000,  # BadCommunicationError
    0x800A0000,  # BadTimeout
    0x800D0000,  # BadServerNotConnected
    0x80310000,  # BadNoCommunication
    0x80320000,  # BadWaitingForInitialData
    0x808A0000,  # BadNotConnected
    0x80AE0000,  # BadConnectionClosed
})


def _communication_quality(value: Any) -> bool:
    if type(value) is int:
        return value in COMMUNICATION_STATUS_CODES
    if isinstance(value, str):
        match = re.search(r"\((0x[0-9a-fA-F]{8})\)$", value)
        return match is not None and int(match.group(1), 16) in COMMUNICATION_STATUS_CODES
    return False


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ProcessSafetyError(f"{name} must be a finite number.")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProcessSafetyError(f"{name} must be a finite number.") from exc
    if not math.isfinite(result):
        raise ProcessSafetyError(f"{name} must be a finite number.")
    return result


def _iso_ns(timestamp_ns: int) -> str:
    seconds, nanos = divmod(timestamp_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


@dataclass(frozen=True)
class RabbitProcessConfig:
    rabbit_url: str
    exchange: str = "idp.bus"
    device_id: str = "crystallizer"
    node_ids: Mapping[str, str] = field(default_factory=lambda: OpcUaNodeConfig().node_ids())
    sample_max_age_s: float = 5.0
    startup_timeout_s: float = 10.0
    command_ttl_s: float = 5.0
    write_timeout_s: float = 5.0
    process_queue: str = "process.telemetry"
    result_queue: str = "process.results"
    measurement_name: str = "crystallizer_raw"
    jacket_min_K: float = 253.15
    jacket_max_K: float = 453.15
    verify_tolerance_K: float = 0.05

    def __post_init__(self) -> None:
        for name in ("rabbit_url", "exchange", "device_id", "process_queue", "result_queue", "measurement_name"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required.")
        if self.process_queue == self.result_queue:
            raise ValueError("Telemetry and write results require separate queues.")
        nodes = self.node_ids if self.node_ids is not None else OpcUaNodeConfig().node_ids()
        if set(nodes) != set(FIELDS) or any(not isinstance(v, str) or not v.strip() for v in nodes.values()):
            raise ValueError("node_ids must map all five process fields to OPC UA NodeIds.")
        if len(set(nodes.values())) != len(FIELDS):
            raise ValueError("Each process field requires a distinct NodeId.")
        object.__setattr__(self, "node_ids", dict(nodes))
        for name in ("sample_max_age_s", "startup_timeout_s", "command_ttl_s", "write_timeout_s", "verify_tolerance_K"):
            if _number(getattr(self, name), name) <= 0:
                raise ValueError(f"{name} must be positive.")
        if not 0 < _number(self.jacket_min_K, "jacket_min_K") < _number(self.jacket_max_K, "jacket_max_K"):
            raise ValueError("Invalid jacket temperature limits.")


@dataclass
class _PendingWrite:
    envelope: dict[str, Any]
    deadline: float
    read_generation: int
    read_timestamp_ns: int
    event: threading.Event = field(default_factory=threading.Event)
    result: ProcessWriteResult | None = None
    error: Exception | None = None
    sent: bool = False


class RabbitMqProcessAdapter(ProcessAdapter):
    """All Pika objects live on one dedicated thread; callers only use a lock/queue."""

    def __init__(
        self,
        config: RabbitProcessConfig,
        *,
        connection_factory: Callable[[str], Any] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        audit_writer=None,
    ) -> None:
        self.config = config
        self.audit_writer = audit_writer
        self.audit_required = False
        self._audit_error: str | None = None
        self._audit_failures: dict[str, int] = {}
        self._connection_factory = connection_factory or self._default_connection
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._condition = threading.Condition(threading.RLock())
        self._outgoing: queue.Queue[_PendingWrite] = queue.Queue()
        self._recovery_outgoing: queue.Queue[dict[str, Any]] = queue.Queue()
        self._write_recovery: dict[str, Any] | None = None
        self._recovery_request: dict[str, Any] | None = None
        self._recovery_last_queued = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._epoch_ns = 0
        self._generation = 0
        self._last_read_generation: int | None = None
        self._last_read_ns: int | None = None
        self._startup_deadline = 0.0
        self._rounds: dict[int, dict[str, float]] = {}
        self._invalid_rounds: set[int] = set()
        self._latest: ProcessState | None = None
        self._latest_ns = 0
        self._negative_sample: dict[str, Any] | None = None
        self._negative_ns = 0
        self._waiting_since: str | None = None
        self._last_invalid_state: dict[str, Any] | None = None
        self._timeout_sample: dict[str, Any] | None = None
        self._timeout_boundary_ns = 0
        self._last_valid_read_at: str | None = None
        self._hard_telemetry_error: str | None = None
        self._newest_seen_ns = 0
        self._newest_fields: set[str] = set()
        self._pending: _PendingWrite | None = None
        self._context: tuple[str, str, int] | None = None
        self._last_command: dict[str, Any] | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._read_count = 0
        self._write_count = 0
        self._reconnect_count = 0
        self._invalid_count = 0
        self._last_write_at: str | None = None

    @staticmethod
    def _default_connection(url: str) -> pika.BlockingConnection:
        parameters = pika.URLParameters(url)
        parameters.connection_attempts = 1
        parameters.retry_delay = 0
        parameters.socket_timeout = 2
        parameters.stack_timeout = 4
        parameters.blocked_connection_timeout = 2
        parameters.heartbeat = 30
        return pika.BlockingConnection(parameters)

    def connect(self) -> None:
        with self._condition:
            if self._connected and self._thread is not None and self._thread.is_alive():
                return
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._startup_deadline = self._monotonic() + self.config.startup_timeout_s
                self._reset_samples_locked(clear_fault=True)
                self._thread = threading.Thread(target=self._io_loop, name="controller-process-io", daemon=True)
                self._thread.start()
            elif self._stop.is_set():
                raise ProcessConnectionError("Previous process I/O connection is still stopping.")
            while not self._connected and not self._stop.is_set():
                remaining = self._startup_deadline - self._monotonic()
                if remaining <= 0:
                    raise self._transport_wait_locked("Reconnecting after process message bus connection timeout.")
                self._condition.wait(remaining)
            if not self._connected:
                raise ProcessConnectionError("Process I/O was stopped before connecting.")

    def disconnect(self) -> None:
        with self._condition:
            self._stop.set()
            self._connected = False
            self._reset_samples_locked(clear_fault=True)
            self._fail_pending_locked("Process I/O disconnected; write result unknown.")
            self._write_recovery = None
            self._recovery_request = None
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4.5)

    def _reset_samples_locked(self, *, clear_fault: bool = False) -> None:
        self._rounds.clear()
        self._invalid_rounds.clear()
        self._latest = None
        self._latest_ns = 0
        self._negative_sample = None
        self._negative_ns = 0
        self._waiting_since = None
        self._timeout_sample = None
        self._timeout_boundary_ns = 0
        self._last_valid_read_at = None
        if clear_fault:
            self._hard_telemetry_error = None
        self._newest_seen_ns = 0
        self._newest_fields.clear()
        self._last_read_generation = None
        self._last_read_ns = None

    def _mark_connected(self) -> None:
        with self._condition:
            self._reset_samples_locked()
            self._epoch_ns = self._clock_ns()
            self._generation += 1
            self._connected = True
            # Any response from the preceding transport generation is stale.
            if self._write_recovery is not None and self._write_recovery.get("pending"):
                self._new_recovery_request_locked()
            self._condition.notify_all()

    def _transport_wait_locked(self, detail: str = "Reconnecting to the process message bus.") -> ProcessTransportPending:
        self._waiting_since = self._waiting_since or _iso_ns(self._clock_ns())
        pending = ProcessTransportPending(waiting_since=self._waiting_since, detail=detail)
        self._last_invalid_state = pending.to_dict()
        return pending

    def _io_loop(self) -> None:
        connected_once = False
        while not self._stop.is_set():
            connection = None
            try:
                connection = self._connection_factory(self.config.rabbit_url)
                channel = connection.channel()
                channel.exchange_declare(exchange=self.config.exchange, exchange_type="topic", durable=True)
                for queue_name, routing_key, callback in (
                    (self.config.process_queue, TELEMETRY_ROUTE, self._on_telemetry),
                    (self.config.result_queue, RESULT_ROUTE, self._on_result),
                ):
                    channel.queue_declare(queue=queue_name, durable=True)
                    channel.queue_bind(queue=queue_name, exchange=self.config.exchange, routing_key=routing_key)
                    channel.basic_consume(queue=queue_name, on_message_callback=callback, auto_ack=False, exclusive=True)
                channel.basic_qos(prefetch_count=100)
                channel.confirm_delivery()
                if connected_once:
                    with self._condition:
                        self._reconnect_count += 1
                connected_once = True
                self._mark_connected()
                while not self._stop.is_set():
                    self._publish_pending(channel)
                    self._publish_recovery(channel)
                    connection.process_data_events(time_limit=0.1)
            except Exception as exc:
                with self._condition:
                    if self._connected:
                        self._startup_deadline = self._monotonic() + self.config.startup_timeout_s
                    self._connected = False
                    self._reset_samples_locked()
                    # Do not include exception text: Pika errors can embed credentials/URLs.
                    self._last_error = f"Process message bus connection failed ({type(exc).__name__})."
                    self._fail_pending_locked("Process message bus connection lost; write result unknown.",
                                              recoverable=self._audit_error is None)
                    self._condition.notify_all()
            finally:
                if connection is not None:
                    try:
                        if connection.is_open:
                            connection.close()
                    except Exception:
                        pass
            self._stop.wait(0.25)

    def read_state(self) -> ProcessState:
        with self._condition:
            while True:
                if self._stop.is_set() or self._thread is None:
                    raise ProcessConnectionError("Process message bus is not connected.")
                if self._hard_telemetry_error is not None:
                    raise ProcessSafetyError(self._hard_telemetry_error)
                if not self._connected:
                    raise self._transport_wait_locked()
                if self._connected:
                    waiting = self._current_measurement_wait_locked()
                    if waiting is not None:
                        raise waiting
                if self._connected and self._latest is not None:
                    age_s = (self._clock_ns() - self._latest_ns) / 1_000_000_000
                    if age_s < 0:
                        raise ProcessSafetyError("Process sample timestamp is in the future.")
                    self._read_count += 1
                    self._last_read_generation = self._generation
                    self._last_read_ns = self._latest_ns
                    return self._latest
                remaining = self._startup_deadline - self._monotonic()
                if remaining <= 0:
                    if self._connected:
                        raise self._begin_timeout_wait_locked()
                    raise self._transport_wait_locked()
                self._condition.wait(remaining)

    def _on_telemetry(self, channel: Any, method: Any, properties: Any, body: bytes) -> None:
        try:
            self._accept_telemetry(json.loads(body))
        except (ValueError, TypeError, OverflowError) as exc:
            with self._condition:
                self._invalid_count += 1
                self._latch_telemetry_fault_locked(f"Invalid process telemetry ({type(exc).__name__}).")
        finally:
            channel.basic_ack(delivery_tag=method.delivery_tag)

    def _measurement_pending_locked(self) -> ProcessMeasurementPending:
        pending = self._current_measurement_wait_locked()
        if pending is None:
            raise ProcessSafetyError("No pending process measurement condition is available.")
        return pending

    def _begin_timeout_wait_locked(self) -> ProcessDataTimeoutPending:
        if self._hard_telemetry_error is not None:
            raise ProcessSafetyError(self._hard_telemetry_error)
        if self._timeout_sample is None:
            self._timeout_boundary_ns = self._clock_ns()
            self._waiting_since = self._waiting_since or _iso_ns(self._timeout_boundary_ns)
            self._timeout_sample = {"waiting_since": self._waiting_since}
            if self._last_valid_read_at is not None:
                self._timeout_sample["last_valid_read_at"] = self._last_valid_read_at
            self._latest = None
            self._negative_sample = None
            self._negative_ns = 0
            self._last_read_generation = None
            self._last_read_ns = None
            pending = ProcessDataTimeoutPending(**self._timeout_sample)
            self._last_invalid_state = pending.to_dict()
            self._last_error = str(pending)
        return ProcessDataTimeoutPending(**self._timeout_sample)

    def _current_measurement_wait_locked(self) -> ProcessMeasurementPending | None:
        if self._hard_telemetry_error is not None:
            raise ProcessSafetyError(self._hard_telemetry_error)
        if self._timeout_sample is not None:
            return ProcessDataTimeoutPending(**self._timeout_sample)
        if self._negative_sample is not None:
            age_s = (self._clock_ns() - self._negative_ns) / 1_000_000_000
            if age_s < 0:
                raise ProcessSafetyError("Process sample timestamp is in the future.")
            if age_s > self.config.sample_max_age_s:
                return self._begin_timeout_wait_locked()
            return ProcessMeasurementPending(**self._negative_sample)
        if self._latest is not None:
            age_s = (self._clock_ns() - self._latest_ns) / 1_000_000_000
            if age_s > self.config.sample_max_age_s:
                return self._begin_timeout_wait_locked()
        elif self._monotonic() >= self._startup_deadline:
            return self._begin_timeout_wait_locked()
        return None

    def _latch_telemetry_fault_locked(self, message: str, *, stamp: int | None = None) -> None:
        self._hard_telemetry_error = message
        self._last_error = message
        self._latest = None
        self._negative_sample = None
        self._negative_ns = 0
        self._timeout_sample = None
        self._waiting_since = None
        self._last_read_generation = None
        self._last_read_ns = None
        self._last_invalid_state = {"reason": "invalid_process_telemetry", "error": message}
        if stamp is not None:
            self._last_invalid_state["read_at"] = _iso_ns(stamp)
        self._condition.notify_all()

    def _cancel_unsent_for_measurement_locked(self, error: ProcessMeasurementPending | None = None) -> None:
        pending = self._pending
        if pending is None or pending.sent or pending.event.is_set():
            return
        error = error or self._measurement_pending_locked()
        self._last_result = {**pending.envelope["payload"], "status": "failed",
                             "error": str(error), "write_result": None, "command_sent": False}
        try:
            self._audit(self._last_result, "not_sent")
        except ProcessConnectionError as exc:
            # A durable archive failure is a fault, not recoverable measurement
            # waiting. The command is still never sent.
            pending.error = exc
        else:
            pending.error = error
        pending.event.set()

    def _accept_telemetry(self, message: Any) -> None:
        if not isinstance(message, dict) or not isinstance(message.get("metrics"), list):
            with self._condition:
                self._latch_telemetry_fault_locked("Process telemetry must contain a metrics JSON array.")
            raise ValueError("Process telemetry must contain a metrics JSON array.")
        with self._condition:
            if self._hard_telemetry_error is not None:
                return
            now_ns = self._clock_ns()
            cutoff = max(self._epoch_ns, self._timeout_boundary_ns,
                         now_ns - int(self.config.sample_max_age_s * 1_000_000_000))
            self._rounds = {ts: values for ts, values in self._rounds.items() if ts >= cutoff}
            self._invalid_rounds = {ts for ts in self._invalid_rounds if ts >= cutoff}
            for metric in message["metrics"]:
                if not isinstance(metric, dict):
                    self._invalid_count += 1
                    self._latch_telemetry_fault_locked("Malformed process metric.")
                    break
                tags = metric.get("tags")
                if not isinstance(tags, dict):
                    if metric.get("name") == self.config.measurement_name:
                        self._invalid_count += 1
                        self._latch_telemetry_fault_locked("Missing process metric identity tags.")
                        break
                    continue
                if tags.get("device") != self.config.device_id:
                    continue
                if metric.get("name") != self.config.measurement_name:
                    continue
                stamp = metric.get("timestamp")
                if isinstance(stamp, bool) or not isinstance(stamp, int):
                    self._invalid_count += 1
                    self._latch_telemetry_fault_locked("Process metric timestamp must be integer nanoseconds.")
                    break
                if (stamp < cutoff or stamp > now_ns
                        or (self._timeout_boundary_ns and stamp <= self._timeout_boundary_ns)):
                    self._invalid_count += 1
                    continue
                if stamp < self._latest_ns or stamp in self._invalid_rounds:
                    continue
                fields = metric.get("fields")
                try:
                    if not isinstance(fields, dict):
                        raise ProcessSafetyError("Missing metric fields.")
                    process_fields = [key for key in FIELDS if key in fields]
                    if len(process_fields) != 1:
                        raise ProcessSafetyError("Each OPC UA metric must identify exactly one process node.")
                    name = process_fields[0]
                    if tags.get("id") != self.config.node_ids[name]:
                        raise ProcessSafetyError(f"Unexpected OPC UA NodeId for {name}.")
                    quality = fields.get("Quality")
                    if _communication_quality(quality):
                        self._invalid_count += 1
                        self._invalid_rounds.add(stamp)
                        self._rounds.pop(stamp, None)
                        waiting = self._begin_timeout_wait_locked()
                        self._last_invalid_state = {**waiting.to_dict(), "communication_quality": quality}
                        self._cancel_unsent_for_measurement_locked(waiting)
                        break
                    if not ((isinstance(quality, str) and quality in GOOD_QUALITY_STRINGS)
                            or (type(quality) is int and quality == 0)):
                        raise ProcessSafetyError(f"Missing or bad OPC UA quality for {name}.")
                    value = _number(fields[name], name)
                    if name == "count_middle" and value < 0:
                        raise ProcessSafetyError(f"Negative process {name}.")
                    if name in ("T", "T_j", "T_j_set") and value <= -CELSIUS_TO_KELVIN:
                        raise ProcessSafetyError(f"Invalid absolute temperature for {name}.")
                    values = self._rounds.setdefault(stamp, {})
                    if name in values and values[name] != value:
                        raise ProcessSafetyError(f"Conflicting duplicate process {name}.")
                    values[name] = value
                    if stamp >= self._newest_seen_ns:
                        self._newest_seen_ns = stamp
                        self._newest_fields = set(values)
                    if len(values) == len(FIELDS) and stamp > self._latest_ns:
                        if values["c"] < 0:
                            # All five fields, node identities, qualities and
                            # other physical constraints have passed. Do not
                            # expose the preceding positive snapshot as fresh.
                            self._latest = None
                            self._timeout_sample = None
                            self._negative_ns = stamp
                            self._waiting_since = self._waiting_since or _iso_ns(stamp)
                            self._negative_sample = {
                                "read_at": _iso_ns(stamp), "raw_c": values["c"],
                                "waiting_since": self._waiting_since,
                            }
                            self._last_invalid_state = {"reason": ProcessMeasurementPending.reason,
                                                        **self._negative_sample}
                            self._last_read_generation = None
                            self._last_read_ns = None
                            self._invalid_count += 1
                            self._last_error = str(self._measurement_pending_locked())
                            self._cancel_unsent_for_measurement_locked()
                        else:
                            self._latest = ProcessState(
                                T=values["T"] + CELSIUS_TO_KELVIN,
                                T_j=values["T_j"] + CELSIUS_TO_KELVIN,
                                c=values["c"], count_middle=values["count_middle"],
                                T_j_set=values["T_j_set"] + CELSIUS_TO_KELVIN,
                                read_at=_iso_ns(stamp), status_codes={key: "OK (0x0)" for key in FIELDS},
                            )
                            self._negative_sample = None
                            self._negative_ns = 0
                            self._waiting_since = None
                            self._timeout_sample = None
                            self._timeout_boundary_ns = 0
                            self._last_valid_read_at = self._latest.read_at
                            self._last_error = None
                        self._latest_ns = stamp
                except ProcessSafetyError as exc:
                    self._invalid_rounds.add(stamp)
                    self._rounds.pop(stamp, None)
                    self._invalid_count += 1
                    self._latch_telemetry_fault_locked(str(exc), stamp=stamp)
                    if stamp >= self._newest_seen_ns:
                        self._newest_seen_ns = stamp
                        self._newest_fields.clear()
                    break
            # Bounds memory even under a producer sending many fresh incomplete rounds.
            for stamp in sorted(self._rounds)[:-128]:
                self._rounds.pop(stamp, None)
            if len(self._invalid_rounds) > 128:
                self._invalid_rounds = set(sorted(self._invalid_rounds)[-128:])
            self._condition.notify_all()

    def set_command_context(self, run_id: str, run_started_at: str, tick_seq: int) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ProcessSafetyError("A run_id is required for process writes.")
        if not isinstance(run_started_at, str) or not run_started_at.strip():
            raise ProcessSafetyError("A run_started_at timestamp is required for process writes.")
        try:
            started = datetime.fromisoformat(run_started_at.replace("Z", "+00:00"))
            if started.tzinfo is None:
                raise ValueError("Timestamp has no timezone.")
        except ValueError as exc:
            raise ProcessSafetyError("Invalid run_started_at timestamp.") from exc
        if isinstance(tick_seq, bool) or not isinstance(tick_seq, int) or tick_seq < 0:
            raise ProcessSafetyError("tick_seq must be a non-negative integer.")
        with self._condition:
            if self._pending is not None and not self._pending.event.is_set():
                raise ProcessSafetyError("Cannot replace the context of an in-flight process write.")
            self._context = (run_id, run_started_at, tick_seq)

    def write_jacket_setpoint(self, setpoint_K: float) -> ProcessWriteResult:
        value = _number(setpoint_K, "setpoint_K")
        if not self.config.jacket_min_K <= value <= self.config.jacket_max_K:
            raise ProcessSafetyError("Jacket setpoint is outside the configured safe range.")
        with self._condition:
            if not self._connected or self._stop.is_set():
                raise self._transport_wait_locked()
            if self._write_recovery is not None:
                raise ProcessWritePending(self._write_recovery)
            if self._pending is not None and not self._pending.event.is_set():
                raise ProcessSafetyError("Only one process write may be in flight.")
            waiting = self._current_measurement_wait_locked()
            if waiting is not None:
                raise waiting
            if self._context is None:
                raise ProcessSafetyError("Process write requires experiment and control-cycle context.")
            if self._last_read_generation != self._generation or self._last_read_ns is None:
                raise ProcessSafetyError("Process write requires a measurement read on the current connection.")
            read_age_s = (self._clock_ns() - self._last_read_ns) / 1_000_000_000
            if read_age_s > self.config.sample_max_age_s:
                raise self._begin_timeout_wait_locked()
            if read_age_s < 0:
                raise ProcessSafetyError("The control measurement timestamp is in the future.")
            run_id, run_started_at, tick_seq = self._context
            command_id = str(uuid.uuid4())
            payload = {
                "command_id": command_id, "run_id": run_id, "run_started_at": run_started_at,
                "tick_seq": tick_seq, "setpoint_K": value,
                "expires_at": _iso_ns(self._clock_ns() + int(self.config.command_ttl_s * 1_000_000_000)),
            }
            envelope = build_envelope("controller", "opcua_writer", "command", "process.setpoint.write", tick_seq, payload, command_id)
            # The command must exist durably before it can reach the hardware.
            self._audit(payload, "prepared")
            pending = _PendingWrite(
                envelope=envelope, deadline=self._monotonic() + self.config.write_timeout_s,
                read_generation=self._last_read_generation, read_timestamp_ns=self._last_read_ns,
            )
            self._pending = pending
            self._last_command = dict(payload)
            self._last_result = None
            self._outgoing.put(pending)
        pending.event.wait(timeout=self.config.write_timeout_s)
        with self._condition:
            if not pending.event.is_set():
                # Only an unsubmitted command may be canceled as measurement
                # waiting. A sent command's missing receipt is always a fault.
                if (not pending.sent and self._connected
                        and pending.read_generation == self._generation):
                    try:
                        waiting = self._current_measurement_wait_locked()
                        read_age_s = (self._clock_ns() - pending.read_timestamp_ns) / 1_000_000_000
                        if waiting is None and read_age_s > self.config.sample_max_age_s:
                            waiting = self._begin_timeout_wait_locked()
                        if waiting is not None:
                            self._cancel_unsent_for_measurement_locked(waiting)
                    except ProcessSafetyError as exc:
                        self._fail_pending_locked(f"{exc} Command not sent.")
                if not pending.event.is_set():
                    self._fail_pending_locked("Timed out waiting for device write acknowledgement; result unknown.", recoverable=True)
            if pending.error is not None:
                raise pending.error
            if pending.result is None:
                raise ProcessConnectionError("Device write result unknown.")
            self._write_count += 1
            self._last_write_at = pending.result.written_at
            return pending.result

    def _publish_pending(self, channel: Any) -> None:
        try:
            pending = self._outgoing.get_nowait()
        except queue.Empty:
            return
        with self._condition:
            if pending is not self._pending or pending.event.is_set():
                return
            if pending.sent:
                return
            if not self._connected or pending.read_generation != self._generation:
                self._fail_pending_locked("Process connection changed before publication; command not sent.", recoverable=True)
                return
            try:
                waiting = self._current_measurement_wait_locked()
                read_age_s = (self._clock_ns() - pending.read_timestamp_ns) / 1_000_000_000
                if waiting is None and read_age_s > self.config.sample_max_age_s:
                    waiting = self._begin_timeout_wait_locked()
                if waiting is not None:
                    self._cancel_unsent_for_measurement_locked(waiting)
                    return
                if read_age_s < 0:
                    raise ProcessSafetyError("Control measurement timestamp is in the future.")
            except ProcessSafetyError as exc:
                self._fail_pending_locked(f"{exc} Command not sent.")
                return
            if self._monotonic() >= pending.deadline:
                self._fail_pending_locked("Write publication deadline expired; command not sent.", recoverable=True)
                return
            pending.sent = True
        # This is invoked only on the owning Pika I/O thread. Never retry publication.
        confirmed = channel.basic_publish(
            exchange=self.config.exchange, routing_key=COMMAND_ROUTE,
            body=json.dumps(pending.envelope, allow_nan=False).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json", delivery_mode=2,
                correlation_id=pending.envelope["correlation_id"],
                expiration=str(max(1, int(self.config.command_ttl_s * 1000))),
            ), mandatory=True,
        )
        if confirmed is False:
            raise ProcessConnectionError("Process command publication was not confirmed.")
        self._audit(pending.envelope["payload"], "published")

    def _audit(self, payload, stage):
        if self.audit_writer is None and not self.audit_required:
            return
        try:
            if self.audit_writer is None:
                raise RuntimeError("Command archive is unavailable.")
            write_process_event(self.audit_writer, payload, "controller", stage, timestamp=self._clock_ns())
            self._audit_error = None
        except Exception as exc:
            self._audit_error = f"Command audit persistence failed ({type(exc).__name__})."
            run_id = str(payload.get("run_id", ""))
            self._audit_failures[run_id] = self._audit_failures.get(run_id, 0) + 1
            raise ProcessConnectionError(self._audit_error) from exc

    def _fail_pending_locked(self, message: str, *, recoverable: bool = False) -> None:
        pending = self._pending
        if pending is not None and not pending.event.is_set():
            pending.error = ProcessConnectionError(message)
            self._last_error = message
            self._last_result = {**pending.envelope["payload"], "status": "unknown", "error": message, "write_result": None}
            try:
                self._audit(self._last_result, "unknown")
            except ProcessConnectionError:
                # The device outcome is still unknown. Never repeat a write due
                # to a local audit failure; surface the archive error separately.
                pass
            if recoverable and self._audit_error is None:
                self._begin_write_recovery_locked(pending, message)
                pending.error = ProcessWritePending(self._write_recovery)
            pending.event.set()

    def _begin_write_recovery_locked(self, pending: _PendingWrite, detail: str) -> None:
        """Latch the original attempt independently of its expired waiter."""
        self._write_recovery = {
            "pending": bool(pending.sent),
            "reason": "process_write_confirmation_pending" if pending.sent else "process_write_not_sent",
            "detail": detail, "waiting_since": _iso_ns(self._clock_ns()),
            "command": copy.deepcopy(pending.envelope["payload"]),
            "command_sent": bool(pending.sent),
            "manual_confirmation_required": False,
            "resolution": None if pending.sent else {"status": "not_sent", "execution_complete": True,
                                                       "recoverable": True},
        }
        self._recovery_request = None
        if pending.sent:
            self._new_recovery_request_locked()
        else:
            self._timeout_sample = None
            self._begin_timeout_wait_locked()

    def _new_recovery_request_locked(self, *, confirm: bool = False) -> None:
        recovery = self._write_recovery
        if recovery is None:
            return
        query_id = str(uuid.uuid4())
        generation = int(recovery.get("recovery_generation", 0)) + 1
        payload = {**copy.deepcopy(recovery["command"]), "query_id": query_id,
                   "recovery_generation": generation}
        if confirm:
            payload["operator_confirmed"] = True
        recovery["query_id"] = query_id
        recovery["recovery_generation"] = generation
        self._recovery_request = build_envelope(
            "controller", "opcua_writer", "command",
            "process.setpoint.confirm" if confirm else "process.setpoint.status",
            payload["tick_seq"], payload, query_id,
        )
        self._recovery_last_queued = float("-inf")

    def write_recovery_status(self) -> dict[str, Any]:
        """Poll nonblocking; all actual AMQP operations remain on the I/O thread."""
        with self._condition:
            recovery = self._write_recovery
            if recovery is None:
                return {"pending": False, "manual_confirmation_required": False, "reason": None, "command": None}
            if recovery.get("resolution_error"):
                raise ProcessSafetyError(recovery["resolution_error"])
            if (recovery.get("pending") and self._connected and not self._stop.is_set()
                    and self._recovery_request is not None
                    and self._monotonic() - self._recovery_last_queued >= max(1.0, self.config.write_timeout_s)):
                self._recovery_outgoing.put(copy.deepcopy(self._recovery_request))
                self._recovery_last_queued = self._monotonic()
            return copy.deepcopy(recovery)

    def confirm_write_recovery(self) -> dict[str, Any]:
        with self._condition:
            if self._write_recovery is None or not self._write_recovery.get("pending"):
                raise ProcessSafetyError("No unresolved process command requires confirmation.")
            if not self._write_recovery.get("manual_confirmation_required"):
                raise ProcessSafetyError("Wait for Writer to identify the uncertain command before confirming it.")
            if not self._connected or self._stop.is_set():
                raise self._transport_wait_locked()
            self._new_recovery_request_locked(confirm=True)
        return self.write_recovery_status()

    def clear_write_recovery(self) -> None:
        """Called only after Controller committed the recovery baseline."""
        with self._condition:
            if self._write_recovery is not None and self._write_recovery.get("pending"):
                raise ProcessSafetyError("Cannot clear an unresolved process command.")
            self._write_recovery = None
            self._recovery_request = None

    def restore_write_recovery(self, snapshot: Mapping[str, Any]) -> None:
        from crystallization_mpc.apps.opcua_writer.commands import WriteCommand

        restored = copy.deepcopy(dict(snapshot))
        command = restored.get("command")
        if not isinstance(command, dict) or type(restored.get("pending")) is not bool:
            raise ValueError("Invalid saved process write recovery state.")
        WriteCommand.from_envelope(build_envelope("controller", "opcua_writer", "command",
            "process.setpoint.write", command.get("tick_seq", 0), command, command.get("command_id")))
        if type(restored.get("manual_confirmation_required")) is not bool:
            raise ValueError("Invalid saved process write confirmation state.")
        with self._condition:
            self._write_recovery = restored
            self._recovery_request = None
            if restored["pending"]:
                self._new_recovery_request_locked()

    def _publish_recovery(self, channel: Any) -> None:
        while True:
            try:
                envelope = self._recovery_outgoing.get_nowait()
            except queue.Empty:
                return
            with self._condition:
                if (self._write_recovery is None or not self._write_recovery.get("pending")
                        or not self._connected or self._stop.is_set()
                        or envelope["correlation_id"] != self._write_recovery.get("query_id")):
                    continue
            break
        confirmed = channel.basic_publish(
            exchange=self.config.exchange, routing_key=COMMAND_ROUTE,
            body=json.dumps(envelope, allow_nan=False).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2,
                correlation_id=envelope["correlation_id"],
                expiration=str(max(1, int(self.config.command_ttl_s * 1000)))), mandatory=True,
        )
        if confirmed is False:
            raise ProcessConnectionError("Process recovery query publication was not confirmed.")

    @staticmethod
    def _matching_command(payload: Mapping[str, Any], command: Mapping[str, Any]) -> bool:
        for key in ("command_id", "run_id", "tick_seq"):
            if payload.get(key) != command.get(key) or type(payload.get(key)) is not type(command.get(key)):
                return False
        try:
            actual = datetime.fromisoformat(payload["run_started_at"].replace("Z", "+00:00"))
            expected = datetime.fromisoformat(command["run_started_at"].replace("Z", "+00:00"))
            return actual.tzinfo is not None and actual == expected
        except (KeyError, TypeError, AttributeError, ValueError):
            return False

    def _validate_success(self, payload: Mapping[str, Any], command: Mapping[str, Any]) -> ProcessWriteResult:
        result = payload.get("write_result")
        if not isinstance(result, dict) or result.get("verified") is not True:
            raise ProcessSafetyError("Device write result does not confirm readback verification.")
        written_K = _number(result.get("setpoint_K"), "write_result.setpoint_K")
        written_C = _number(result.get("setpoint_C"), "write_result.setpoint_C")
        readback_K = _number(result.get("readback_K"), "write_result.readback_K")
        if (abs(written_K - command["setpoint_K"]) > 1e-9
                or abs(written_K - written_C - CELSIUS_TO_KELVIN) > 1e-9
                or abs(readback_K - written_K) > self.config.verify_tolerance_K):
            raise ProcessSafetyError("Device write result does not match the requested setpoint/readback.")
        written_at = result.get("written_at")
        try:
            parsed = datetime.fromisoformat(written_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Missing timezone.")
        except (AttributeError, ValueError) as exc:
            raise ProcessSafetyError("Device write result has an invalid written_at timestamp.") from exc
        return ProcessWriteResult(written_K, written_C, written_at, True, readback_K)

    def _accept_recovery_result(self, envelope: Mapping[str, Any]) -> None:
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return
        with self._condition:
            recovery = self._write_recovery
            if (recovery is None or not recovery.get("pending")
                    or envelope.get("correlation_id") != recovery.get("query_id")
                    or payload.get("query_id") != recovery.get("query_id")
                    or type(payload.get("recovery_generation")) is not int
                    or payload.get("recovery_generation") != recovery.get("recovery_generation")
                    or not self._matching_command(payload, recovery["command"])):
                return
            for name in ("execution_complete", "manual_confirmation_required", "recoverable"):
                if type(payload.get(name)) is not bool:
                    return
            if payload.get("status") not in {"success", "failed", "unknown", "executing"}:
                return
            if (payload["status"] == "unknown" and payload["recoverable"]
                    and payload.get("confirmed_current_state") is not True):
                return
            if (payload["status"] == "failed" and payload["recoverable"]
                    and payload.get("failure_kind") not in {"communication", "cancelled"}):
                return
            self._audit(payload, f"recovery_receipt:{payload['query_id']}:{payload['status']}:"
                        f"{int(payload['execution_complete'])}:{int(payload['manual_confirmation_required'])}")
            if payload["status"] == "success":
                try:
                    self._validate_success(payload, recovery["command"])
                except ProcessSafetyError as exc:
                    recovery["resolution_error"] = str(exc)
                    return
            recovery["resolution"] = copy.deepcopy(payload)
            recovery["manual_confirmation_required"] = payload["manual_confirmation_required"]
            if payload["execution_complete"] and payload["recoverable"] and not payload["manual_confirmation_required"]:
                recovery["pending"] = False
                recovery["reason"] = "process_write_resolved"
                # Recovery measurements must be newer than the confirmation,
                # even if a data timeout had already established an older floor.
                self._timeout_sample = None
                self._begin_timeout_wait_locked()
            elif payload["execution_complete"] and not payload["recoverable"] and not payload["manual_confirmation_required"]:
                recovery["resolution_error"] = payload.get("error") or "Writer rejected the previous device command."
            self._condition.notify_all()

    def _on_result(self, channel: Any, method: Any, properties: Any, body: bytes) -> None:
        try:
            self._accept_result(json.loads(body))
        except (ValueError, TypeError, OverflowError):
            with self._condition:
                self._last_error = "Invalid process write result JSON."
        except ProcessConnectionError:
            # Keep a valid result available for audit retry after disk recovery.
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return
        channel.basic_ack(delivery_tag=method.delivery_tag)

    def _accept_result(self, envelope: Any) -> None:
        if (not isinstance(envelope, dict) or envelope.get("ver") != 1
                or envelope.get("msg_type") != "event"
                or envelope.get("src") != "opcua_writer" or envelope.get("dst") != "process"):
            return
        if envelope.get("name") == "process.setpoint.status.result":
            self._accept_recovery_result(envelope)
            return
        if envelope.get("name") != "process.setpoint.result":
            return
        payload = envelope.get("payload")
        if (not isinstance(payload, dict) or not isinstance(payload.get("command_id"), str)
                or not isinstance(payload.get("run_id"), str) or type(payload.get("tick_seq")) is not int
                or payload.get("status") not in ("success", "failed", "unknown")
                or envelope.get("correlation_id") != payload.get("command_id")):
            return
        # Retain late/replayed receipts without rewriting a timed-out cycle.
        self._audit(payload, "receipt")
        with self._condition:
            pending = self._pending
            if pending is None or pending.event.is_set():
                # A late original receipt is retained as evidence. A read-only
                # Writer status query fences the queue and resolves the wait.
                return
            command = pending.envelope["payload"]
            if not self._matching_command(payload, command):
                return
            if self._monotonic() >= pending.deadline:
                self._fail_pending_locked("Device acknowledgement arrived after deadline; result unknown.", recoverable=True)
                return
            status = payload["status"]
            self._last_result = dict(payload)
            try:
                if status == "unknown" or (status == "failed" and payload.get("recoverable") is True):
                    self._begin_write_recovery_locked(pending, payload.get("error") or "Device communication interrupted.")
                    pending.error = ProcessWritePending(self._write_recovery)
                elif status != "success":
                    raise ProcessConnectionError(f"Device write {status}: {payload.get('error') or 'No reason supplied.'}")
                else:
                    pending.result = self._validate_success(payload, command)
                    self._last_error = None
            except (ProcessConnectionError, ProcessSafetyError) as exc:
                pending.error = exc
                self._last_error = str(exc)
                if status == "success":
                    self._last_result = {**payload, "status": "failed", "error": str(exc)}
            pending.event.set()
            self._condition.notify_all()

    def status(self) -> Mapping[str, Any]:
        with self._condition:
            age_s = (self._clock_ns() - self._latest_ns) / 1_000_000_000 if self._latest is not None else None
            negative_age_s = ((self._clock_ns() - self._negative_ns) / 1_000_000_000
                              if self._negative_sample is not None else None)
            waiting = (self._connected and self._hard_telemetry_error is None and (
                self._timeout_sample is not None or (negative_age_s is not None
                    and 0 <= negative_age_s <= self.config.sample_max_age_s)))
            wait_metadata = ((ProcessDataTimeoutPending(**self._timeout_sample).to_dict()
                              if self._timeout_sample is not None else self._last_invalid_state)
                             if waiting else None)
            return {
                "class": f"{type(self).__module__}.{type(self).__qualname__}",
                "safe_noop": False, "transport": "rabbitmq", "connected": self._connected,
                "device_id": self.config.device_id,
                "node_ids": dict(self.config.node_ids),
                "read_count": self._read_count, "write_count": self._write_count,
                "reconnect_count": self._reconnect_count, "invalid_message_count": self._invalid_count,
                "last_state": self._latest.to_dict() if self._latest is not None else None,
                "last_read_at": self._latest.read_at if self._latest is not None else None,
                "collection_timestamp_ns": self._latest_ns if self._latest is not None else None,
                "last_used_timestamp_ns": self._last_read_ns,
                "sample_age_s": age_s,
                "waiting_for_data": waiting,
                "wait_reason": wait_metadata.get("reason") if wait_metadata else None,
                "waiting_since": self._waiting_since if waiting else None,
                "last_invalid_state": dict(self._last_invalid_state) if self._last_invalid_state else None,
                "sample_valid": (self._connected and self._hard_telemetry_error is None
                                 and self._timeout_sample is None and self._negative_sample is None
                                 and age_s is not None and 0 <= age_s <= self.config.sample_max_age_s),
                "hard_data_error": self._hard_telemetry_error,
                "missing_fields": [name for name in FIELDS if name not in self._newest_fields],
                "last_command": dict(self._last_command) if self._last_command else None,
                "last_write_result": dict(self._last_result) if self._last_result else None,
                "write_in_flight": self._pending is not None and not self._pending.event.is_set(),
                "write_recovery": copy.deepcopy(self._write_recovery),
                "reconnecting": not self._connected and self._thread is not None and not self._stop.is_set(),
                "last_write_at": self._last_write_at, "last_error": self._last_error,
                "audit_error": self._audit_error,
                "audit_enabled": self.audit_writer is not None,
                "audit_failure_count": self._audit_failures.get(self._context[0], 0) if self._context else 0,
                "safe_jacket_range_K": [self.config.jacket_min_K, self.config.jacket_max_K],
            }


# Keep the concise spelling available for composition and external integrations.
RabbitProcessAdapter = RabbitMqProcessAdapter
