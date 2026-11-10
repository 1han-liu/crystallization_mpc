"""RabbitMQ process I/O, independent of the Controller business-message thread.

Each process field retains its latest finite, Good-quality value. Reads take
a nonblocking snapshot; an IR update is independent of the control clock.
Source timestamps remain observation metadata, never a freshness gate.
"""

from __future__ import annotations

import json
import copy
import math
import queue
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
    ControllerReadError, ProcessWriteError,
    OpcUaNodeConfig,
    ProcessAdapter,
    ProcessConnectionError,
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
    command_ttl_s: float = 5.0
    write_timeout_s: float = 5.0
    process_queue: str = "process.telemetry"
    result_queue: str = "process.results"
    measurement_name: str = "crystallizer_raw"

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
        for name in ("command_ttl_s", "write_timeout_s"):
            if _number(getattr(self, name), name) <= 0:
                raise ValueError(f"{name} must be positive.")


@dataclass
class _PendingWrite:
    envelope: dict[str, Any]
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
        audit_writer=None,
    ) -> None:
        self.config = config
        self.audit_writer = audit_writer
        self._audit_error: str | None = None
        self._audit_failures: dict[str, int] = {}
        self._connection_factory = connection_factory or self._default_connection
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._outgoing: queue.Queue[_PendingWrite] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._latest_fields: dict[str, dict[str, Any]] = {}
        self._field_errors: dict[str, dict[str, Any]] = {}
        self._last_read_ns: int | None = None
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
        """Start the existing I/O worker without waiting under the service lock."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    raise ProcessConnectionError("Previous process I/O connection is still stopping.")
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._io_loop, name="controller-process-io", daemon=True)
            self._thread.start()

    def disconnect(self) -> None:
        with self._lock:
            self._stop.set()
            self._connected = False
            self._latest_fields.clear()
            self._field_errors.clear()
            self._last_read_ns = None
            self._fail_pending_locked("Process I/O disconnected; write result unknown.")
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4.5)



    def _mark_connected(self):
        with self._lock:
            self._connected = True
            self._last_error = None

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
                    with self._lock:
                        self._reconnect_count += 1
                connected_once = True
                self._mark_connected()
                while not self._stop.is_set():
                    self._publish_pending(channel)
                    connection.process_data_events(time_limit=0.1)
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    # Do not include exception text: Pika errors can embed credentials/URLs.
                    self._last_error = f"Process message bus connection failed ({type(exc).__name__})."
                    self._fail_pending_locked("Process message bus connection lost; write result unknown.")
            finally:
                if connection is not None:
                    try:
                        if connection.is_open:
                            connection.close()
                    except Exception:
                        pass
            self._stop.wait(0.25)

    def read_state(self) -> ProcessState:
        """Copy the latest available fields once, without consuming or aging them."""
        with self._lock:
            missing = [name for name in FIELDS if name not in self._latest_fields]
            if missing:
                raise ControllerReadError("Waiting for initial process data: " + ", ".join(missing) + ".")
            snapshot = copy.deepcopy(self._latest_fields)
            read_ns = self._clock_ns()
            self._read_count += 1
            self._last_read_ns = read_ns
        values = {name: item["value"] for name, item in snapshot.items()}
        source_times = {name: _iso_ns(item["timestamp_ns"]) for name, item in snapshot.items()
                        if item["basis"] == "source" and item["timestamp_ns"] is not None}
        bases = {item["basis"] for item in snapshot.values()}
        basis = next(iter(bases)) if len(bases) == 1 else "mixed"
        stamps = {item["timestamp_ns"] for item in snapshot.values()}
        collection_ns = next(iter(stamps)) if basis == "gather" and len(stamps) == 1 else None
        return ProcessState(
            T=values["T"] + CELSIUS_TO_KELVIN,
            T_j=values["T_j"] + CELSIUS_TO_KELVIN,
            T_j_set=values["T_j_set"] + CELSIUS_TO_KELVIN,
            c=values["c"], count_middle=values["count_middle"], read_at=_iso_ns(read_ns),
            source_timestamps=source_times,
            status_codes={name: item["quality"] for name, item in snapshot.items()},
            collection_timestamp_ns=collection_ns, timestamp_basis=basis,
            received_at=_iso_ns(max(item["received_ns"] for item in snapshot.values())),
        )

    def _on_telemetry(self, channel: Any, method: Any, properties: Any, body: bytes) -> None:
        try:
            self._accept_telemetry(json.loads(body))
        except (ValueError, TypeError, OverflowError) as exc:
            with self._lock:
                self._invalid_count += 1
                self._last_error = f"Invalid process telemetry ({type(exc).__name__})."
        finally:
            channel.basic_ack(delivery_tag=method.delivery_tag)


    def _accept_telemetry(self, message):
        if not isinstance(message, dict) or not isinstance(message.get("metrics"), list):
            raise ValueError("Process telemetry requires a metrics array.")
        with self._lock:
            received_ns = self._clock_ns()
            for metric in message["metrics"]:
                if not isinstance(metric, dict) or metric.get("name") != self.config.measurement_name:
                    continue
                tags, fields = metric.get("tags"), metric.get("fields")
                if (not isinstance(tags, dict) or tags.get("device") != self.config.device_id
                        or not isinstance(fields, dict)):
                    continue
                for name in FIELDS:
                    node_id = self.config.node_ids[name]
                    if name not in fields and tags.get("id") != node_id:
                        continue
                    quality = fields.get("Quality")
                    error = None
                    if tags.get("id") != node_id:
                        error = f"Unexpected OPC UA NodeId for {name}."
                    elif name not in fields:
                        error = f"Telegraf omitted the value for {name}."
                    elif not ((type(quality) is int and quality == 0)
                              or (isinstance(quality, str) and quality in GOOD_QUALITY_STRINGS)):
                        error = f"Telegraf reported read quality {quality!r} for {name}."
                    else:
                        try:
                            value = _number(fields[name], name)
                        except ProcessSafetyError as exc:
                            error = str(exc)
                    if error is not None:
                        self._invalid_count += 1
                        self._field_errors[name] = {"error": error, "quality": quality,
                                                    "received_at": _iso_ns(received_ns)}
                        continue
                    stamp = metric.get("timestamp")
                    stamp = stamp if type(stamp) is int and 0 < stamp <= 2**63 - 1 else None
                    basis = tags.get("timestamp_basis")
                    basis = basis if basis in ("source", "gather") else "unknown"
                    self._latest_fields[name] = {"value": value, "timestamp_ns": stamp,
                        "basis": basis, "quality": quality, "received_ns": received_ns}
                    self._field_errors.pop(name, None)

    def set_command_context(self,run_id,run_started_at,tick_seq):
        with self._lock:
            self._context=(run_id,run_started_at,tick_seq)

    def write_jacket_setpoint(self,setpoint_K):
        with self._lock:
            if not self._connected or self._stop.is_set():
                raise ProcessWriteError('Process message bus is disconnected.',command_sent=False)
            if self._context is None:
                raise ProcessWriteError('Writer envelope has no experiment/cycle identity.',command_sent=False)
            run_id,started,tick=self._context
            command_id=str(uuid.uuid4())
            payload={'command_id':command_id,'run_id':run_id,'run_started_at':started,'tick_seq':tick,
                     'setpoint_K':setpoint_K,'expires_at':_iso_ns(self._clock_ns()+int(self.config.command_ttl_s*1e9))}
            envelope=build_envelope('controller','opcua_writer','command','process.setpoint.write',tick,payload,command_id)
            pending=_PendingWrite(envelope=envelope)
            self._pending=pending
            self._last_command=copy.deepcopy(payload)
            self._last_result=None
            self._outgoing.put(pending)
        pending.event.wait(timeout=self.config.write_timeout_s)
        with self._lock:
            if not pending.event.is_set():
                pending.error=ProcessWriteError('Writer call did not return before its existing timeout; result unknown.',
                                                command_sent=pending.sent)
                pending.event.set()
                self._last_result={**payload,'status':'unknown','error':str(pending.error)}
            if self._pending is pending:
                self._pending=None
            if pending.error is not None:
                raise pending.error
            if pending.result is None:
                raise ProcessWriteError('Writer returned no result.',command_sent=pending.sent)
            self._write_count+=1
            self._last_write_at=pending.result.written_at
            return pending.result

    def _publish_pending(self,channel):
        try: pending=self._outgoing.get_nowait()
        except queue.Empty: return
        with self._lock:
            if pending is not self._pending or pending.event.is_set():return
            if pending.sent:return
            if not self._connected:
                self._fail_pending_locked('Process message bus disconnected before publication.')
                return
        # Exactly one publication; no replay, status query or readback request.
        try:
            body=json.dumps(pending.envelope,allow_nan=False).encode('utf-8')
            with self._lock:
                if pending.event.is_set(): return
                pending.sent=True
            confirmed=channel.basic_publish(exchange=self.config.exchange,routing_key=COMMAND_ROUTE,body=body,
                properties=pika.BasicProperties(content_type='application/json',delivery_mode=2,
                    correlation_id=pending.envelope['correlation_id'],
                    expiration=str(max(1,int(self.config.command_ttl_s*1000)))),mandatory=True)
            if confirmed is False:raise ProcessConnectionError('Message publication was not confirmed.')
        except Exception as exc:
            with self._lock:self._fail_pending_locked(str(exc))
            return
        self._audit(pending.envelope['payload'],'published')

    def _audit(self,payload,stage):
        if self.audit_writer is None:return
        try:
            write_process_event(self.audit_writer,payload,'controller',stage,timestamp=self._clock_ns())
            self._audit_error=None
        except Exception as exc:
            self._audit_error=f'Command observation failed ({type(exc).__name__}).'
            run_id=str(payload.get('run_id',''))
            self._audit_failures[run_id]=self._audit_failures.get(run_id,0)+1


    def _fail_pending_locked(self,message):
        pending=self._pending
        if pending is not None and not pending.event.is_set():
            pending.error=ProcessWriteError(message,command_sent=pending.sent)
            pending.event.set()
            self._last_result={**pending.envelope['payload'],'status':'unknown','error':message}
        self._last_error=message

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

    @staticmethod
    def _validate_success(payload):
        result = payload.get('write_result')
        if not isinstance(result, dict):
            raise ProcessWriteError('Writer success receipt omitted its write result.', command_sent=True)
        return ProcessWriteResult(setpoint_K=result['setpoint_K'], setpoint_C=result['setpoint_C'],
            written_at=result['written_at'], verified=result.get('verified', False),
            readback_K=result.get('readback_K'))


    def _on_result(self, channel, method, properties, body):
        try:
            self._accept_result(json.loads(body))
        except (ValueError, TypeError, OverflowError) as exc:
            with self._lock:
                self._last_error = f'Invalid Writer receipt ({type(exc).__name__}).'
        finally:
            channel.basic_ack(delivery_tag=method.delivery_tag)


    def _accept_result(self,envelope):
        if not isinstance(envelope,dict) or envelope.get('src')!='opcua_writer' or envelope.get('name')!='process.setpoint.result':return
        payload=envelope.get('payload')
        if not isinstance(payload,dict):return
        self._audit(payload,'receipt')
        with self._lock:
            pending=self._pending
            if pending is None or pending.event.is_set():return
            command=pending.envelope['payload']
            if not self._matching_command(payload,command):return
            self._last_result=copy.deepcopy(payload)
            if payload.get('status')=='success':
                try:
                    pending.result=self._validate_success(payload)
                except Exception as exc:
                    pending.error=ProcessWriteError(str(exc),command_sent=pending.sent)
            else:
                pending.error=ProcessWriteError(payload.get('error') or 'Writer returned a failure.',
                    write_outcome=payload.get('status','unknown'),command_sent=pending.sent)
            pending.event.set()


    def status(self):
        with self._lock:
            missing = [name for name in FIELDS if name not in self._latest_fields]
            return {"enabled": True, "transport": "rabbitmq", "connected": self._connected,
                "read_count": self._read_count, "write_count": self._write_count,
                "reconnect_count": self._reconnect_count, "last_error": self._last_error,
                "waiting_for_data": bool(missing),
                "wait_reason": "initial_process_data" if missing else None,
                "available_fields": sorted(self._latest_fields), "missing_fields": missing,
                "field_errors": copy.deepcopy(self._field_errors), "invalid_count": self._invalid_count,
                "last_read_at": _iso_ns(self._last_read_ns) if self._last_read_ns is not None else None,
                "last_command": copy.deepcopy(self._last_command),
                "last_result": copy.deepcopy(self._last_result), "last_write_at": self._last_write_at,
                "audit_failure_count": sum(self._audit_failures.values()), "audit_error": self._audit_error,
                "limitations": ["Cached fields update independently; this is not synchronous OPC UA readValue.",
                                "Source timestamps describe measurements, not the control clock."]}


RabbitProcessAdapter = RabbitMqProcessAdapter
