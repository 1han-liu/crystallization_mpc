"""RabbitMQ process I/O, independent of the Controller business-message thread.

Telegraf fields are consumed independently at source read positions. Unread
deliveries are not evidence of synchronous OPC UA readValue; no delivery is
reused after consumption. Writer calls finish once and do not gate later cycles.
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
    ControllerReadError, ProcessWriteError, MATLAB_EMPTY,
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
        for name in ("startup_timeout_s", "command_ttl_s", "write_timeout_s"):
            if _number(getattr(self, name), name) <= 0:
                raise ValueError(f"{name} must be positive.")


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
        self._field_results = {}
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
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4.5)

    def _reset_samples_locked(self, *, clear_fault=False):
        self._field_results = {}
        self._latest = None
        self._last_read_ns = None
        self._hard_telemetry_error = None
        self._condition.notify_all()


    def _mark_connected(self):
        with self._condition:
            self._reset_samples_locked()
            self._generation += 1
            self._connected = True
            self._condition.notify_all()


    def _transport_wait_locked(self, detail='Process message bus is disconnected.'):
        return ProcessConnectionError(detail)


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

    def read_state(self):
        values = {name:self.read_initial_field(name) for name in ('T','T_j','T_j_set','c','count_middle')}
        return ProcessState(T=values['T']+CELSIUS_TO_KELVIN,T_j=values['T_j']+CELSIUS_TO_KELVIN,
            T_j_set=values['T_j_set']+CELSIUS_TO_KELVIN,c=values['c'],count_middle=values['count_middle'],
            read_at=_iso_ns(self._clock_ns()))


    def _on_telemetry(self, channel: Any, method: Any, properties: Any, body: bytes) -> None:
        try:
            self._accept_telemetry(json.loads(body))
        except (ValueError, TypeError, OverflowError) as exc:
            with self._condition:
                self._invalid_count += 1
                self._last_error = f"Invalid process telemetry ({type(exc).__name__})."
        finally:
            channel.basic_ack(delivery_tag=method.delivery_tag)


    def _accept_telemetry(self, message):
        if not isinstance(message,dict) or not isinstance(message.get('metrics'),list):
            raise ValueError('Process telemetry requires a metrics array.')
        with self._condition:
            for metric in message['metrics']:
                if not isinstance(metric,dict):
                    continue
                tags,fields = metric.get('tags',{}),metric.get('fields',{})
                if metric.get('name')!=self.config.measurement_name or tags.get('device')!=self.config.device_id:
                    continue
                if not isinstance(fields,dict):
                    continue
                for name in FIELDS:
                    if name not in fields:
                        continue
                    # Routing identity is representation, not a physical-value gate.
                    if tags.get('id')!=self.config.node_ids[name]:
                        result = ControllerReadError(f'Unexpected OPC UA NodeId for {name}.')
                    else:
                        quality = fields.get('Quality')
                        good = quality in GOOD_QUALITY_STRINGS if isinstance(quality,str) else type(quality) is int and quality==0
                        result = copy.deepcopy(fields[name]) if good else ControllerReadError(f'Telegraf reported read quality {quality!r} for {name}.')
                    # Only unread deliveries are stored. A consumed value is never
                    # reused as proof of another successful read. Timestamp age and
                    # cross-field collection rounds do not decide admissibility.
                    self._field_results[name] = (result, metric.get('timestamp'))
            self._condition.notify_all()


    def set_command_context(self,run_id,run_started_at,tick_seq):
        with self._condition:
            self._context=(run_id,run_started_at,tick_seq)


    def write_jacket_setpoint(self,setpoint_K):
        with self._condition:
            if not self._connected or self._stop.is_set():
                raise ProcessWriteError('Process message bus is disconnected.',command_sent=False)
            if self._context is None:
                raise ProcessWriteError('Writer envelope has no experiment/cycle identity.',command_sent=False)
            run_id,started,tick=self._context
            command_id=str(uuid.uuid4())
            payload={'command_id':command_id,'run_id':run_id,'run_started_at':started,'tick_seq':tick,
                     'setpoint_K':setpoint_K,'expires_at':_iso_ns(self._clock_ns()+int(self.config.command_ttl_s*1e9))}
            envelope=build_envelope('controller','opcua_writer','command','process.setpoint.write',tick,payload,command_id)
            pending=_PendingWrite(envelope=envelope,deadline=self._monotonic()+self.config.write_timeout_s,
                                  read_generation=self._generation,read_timestamp_ns=self._clock_ns())
            self._pending=pending
            self._last_command=copy.deepcopy(payload)
            self._last_result=None
            self._outgoing.put(pending)
        pending.event.wait(timeout=self.config.write_timeout_s)
        with self._condition:
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
        with self._condition:
            if pending is not self._pending or pending.event.is_set():return
            if pending.sent:return
            if not self._connected:
                self._fail_pending_locked('Process message bus disconnected before publication.')
                return
        # Exactly one publication; no replay, status query or readback request.
        try:
            body=json.dumps(pending.envelope,allow_nan=False).encode('utf-8')
            with self._condition:
                if pending.event.is_set(): return
                pending.sent=True
            confirmed=channel.basic_publish(exchange=self.config.exchange,routing_key=COMMAND_ROUTE,body=body,
                properties=pika.BasicProperties(content_type='application/json',delivery_mode=2,
                    correlation_id=pending.envelope['correlation_id'],
                    expiration=str(max(1,int(self.config.command_ttl_s*1000)))),mandatory=True)
            if confirmed is False:raise ProcessConnectionError('Message publication was not confirmed.')
        except Exception as exc:
            with self._condition:self._fail_pending_locked(str(exc))
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


    def _fail_pending_locked(self,message,*,recoverable=False):
        pending=self._pending
        if pending is not None and not pending.event.is_set():
            pending.error=ProcessWriteError(message,command_sent=pending.sent)
            pending.event.set()
            self._last_result={**pending.envelope['payload'],'status':'unknown','error':message}
        self._last_error=message
        self._condition.notify_all()


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

    def _validate_success(self, payload, command):
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
            with self._condition:
                self._last_error = f'Invalid Writer receipt ({type(exc).__name__}).'
        finally:
            channel.basic_ack(delivery_tag=method.delivery_tag)


    def _accept_result(self,envelope):
        if not isinstance(envelope,dict) or envelope.get('src')!='opcua_writer' or envelope.get('name')!='process.setpoint.result':return
        payload=envelope.get('payload')
        if not isinstance(payload,dict):return
        self._audit(payload,'receipt')
        with self._condition:
            pending=self._pending
            if pending is None or pending.event.is_set():return
            command=pending.envelope['payload']
            if not self._matching_command(payload,command):return
            self._last_result=copy.deepcopy(payload)
            if payload.get('status')=='success':
                try:
                    pending.result=self._validate_success(payload,command)
                except Exception as exc:
                    pending.error=ProcessWriteError(str(exc),command_sent=pending.sent)
            else:
                pending.error=ProcessWriteError(payload.get('error') or 'Writer returned a failure.',
                    write_outcome=payload.get('status','unknown'),command_sent=pending.sent)
            pending.event.set()
            self._condition.notify_all()


    def status(self):
        with self._condition:
            return {'enabled':True,'transport':'rabbitmq','connected':self._connected,
                'read_count':self._read_count,'write_count':self._write_count,
                'reconnect_count':self._reconnect_count,'last_error':self._last_error,
                'waiting_for_data':False,'write_recovery':None,'readback_required':False,
                'unread_fields':sorted(self._field_results),'last_command':copy.deepcopy(self._last_command),
                'last_result':copy.deepcopy(self._last_result),'last_write_at':self._last_write_at,
                'audit_failure_count':sum(self._audit_failures.values()),'audit_error':self._audit_error,
                'limitations':['Telegraf delivery is not synchronous OPC UA readValue.',
                              'Writer-side validation and command expiry remain external.']}


# Keep the concise spelling available for composition and external integrations.
    def read_process_field(self, name):
        try:
            return self._read_field(name)
        except Exception as exc:
            if name in ('T', 'T_j'):
                error = ControllerReadError('Output argument "value" not assigned a value in read_value.')
                error.reference_identifier = 'MATLAB:unassignedOutputs'
                raise error from exc
            raise


    def _read_field(self, name):
        deadline = self._monotonic()+self.config.startup_timeout_s
        with self._condition:
            while name not in self._field_results:
                if self._stop.is_set() or not self._connected:
                    raise ControllerReadError('Process message bus is disconnected; no OPC UA read result is available.')
                remaining = deadline-self._monotonic()
                if remaining<=0:
                    raise ControllerReadError(f'No unread Telegraf result for {name}; OPC UA call outcome is unobserved.')
                self._condition.wait(remaining)
            result,stamp = self._field_results.pop(name)
            if isinstance(result,Exception):
                raise result
            self._read_count += 1
            self._last_read_ns = stamp
            self._last_read_generation = self._generation
            if result is None or (isinstance(result,(tuple,list)) and len(result)==0):
                return MATLAB_EMPTY
            return result


    def read_initial_field(self, name):
        return self._read_field(name)


RabbitProcessAdapter = RabbitMqProcessAdapter
