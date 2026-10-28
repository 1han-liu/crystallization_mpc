"""Read-only, run-independent observation of the Telegraf process stream.

The exclusive queue receives a copy of telemetry, never consumes Controller's
queue, and has no equipment-command or write-result binding.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from numbers import Real

import pika

from crystallization_mpc.apps.controller.process import CELSIUS_TO_KELVIN, OpcUaNodeConfig
from crystallization_mpc.apps.controller.rabbit_process import (
    FIELDS, GOOD_QUALITY_STRINGS, TELEMETRY_ROUTE, RabbitProcessConfig,
)


class ProcessSourceMonitor:
    def __init__(self, config: RabbitProcessConfig, *, enabled=True,
                 clock_ns=time.time_ns, connection_factory=None):
        self.config = config
        self.enabled = enabled
        self._clock_ns = clock_ns
        self._connection_factory = connection_factory or self._connect
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._connected = False
        self._epoch_ns = 0
        self._rounds = {}
        self._invalid_rounds = set()
        self._sample = None
        self._sample_ns = 0
        self._newest_ns = 0
        self._problem_ns = 0
        self._problem = None
        self._connection_error = None

    @classmethod
    def from_env(cls, rabbit_url, exchange):
        defaults = OpcUaNodeConfig()
        nodes = OpcUaNodeConfig(
            namespace_index=int(os.getenv("PROCESS_NAMESPACE_INDEX", "2")),
            **{name: os.getenv(f"PROCESS_{name.upper()}_NODE", getattr(defaults, name))
               for name in FIELDS},
        )
        return cls(RabbitProcessConfig(
            rabbit_url=rabbit_url, exchange=exchange, node_ids=nodes.node_ids(),
            device_id=os.getenv("PROCESS_DEVICE_ID", "crystallizer"),
            measurement_name=os.getenv("PROCESS_MEASUREMENT_NAME", "crystallizer_raw"),
            sample_max_age_s=float(os.getenv("PROCESS_SAMPLE_MAX_AGE_S", "5")),
        ), enabled=os.getenv("CENTRAL_PROCESS_MONITOR_ENABLED", "true").lower() == "true")

    @staticmethod
    def _connect(url):
        params = pika.URLParameters(url)
        params.connection_attempts = 1
        params.socket_timeout = 2
        params.stack_timeout = 4
        params.blocked_connection_timeout = 2
        params.heartbeat = 30
        return pika.BlockingConnection(params)

    def start(self):
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="central-process-source", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _set_connection(self, connected, error=None):
        with self._lock:
            self._connected = connected
            self._connection_error = error
            if connected:
                # Reconnection requires a new collection, not a cached sample.
                self._epoch_ns = self._clock_ns()
                self._rounds.clear()
                self._invalid_rounds.clear()
                self._sample = None
                self._sample_ns = self._newest_ns = self._problem_ns = 0
                self._problem = None

    def _run(self):
        while not self._stop.is_set():
            connection = None
            try:
                connection = self._connection_factory(self.config.rabbit_url)
                channel = connection.channel()
                channel.exchange_declare(exchange=self.config.exchange, exchange_type="topic", durable=True)
                declared = channel.queue_declare(
                    queue="", exclusive=True, auto_delete=True,
                    arguments={"x-message-ttl": max(1, int(self.config.sample_max_age_s * 1000)),
                               "x-max-length": 1000},
                )
                channel.queue_bind(exchange=self.config.exchange, queue=declared.method.queue,
                                   routing_key=TELEMETRY_ROUTE)
                channel.basic_consume(queue=declared.method.queue, on_message_callback=self._on_message,
                                      auto_ack=True)
                self._set_connection(True)
                while not self._stop.is_set() and connection.is_open:
                    connection.process_data_events(time_limit=0.25)
            except Exception:
                # Never publish connection strings or credentials in UI errors.
                self._set_connection(False, "Cannot receive process telemetry from RabbitMQ.")
            finally:
                self._set_connection(False, "Process telemetry connection is unavailable.")
                if connection is not None and connection.is_open:
                    try:
                        connection.close()
                    except Exception:
                        pass
            self._stop.wait(2)

    def _on_message(self, _channel, _method, _properties, body):
        try:
            message = json.loads(body)
        except (ValueError, UnicodeError):
            message = None
        self.accept(message)

    def _reject(self, stamp, message):
        self._invalid_rounds.add(stamp)
        if len(self._invalid_rounds) > 128:
            self._invalid_rounds = set(sorted(self._invalid_rounds)[-128:])
        self._rounds.pop(stamp, None)
        if stamp >= self._problem_ns:
            self._problem_ns = stamp
            self._problem = message

    def accept(self, message):
        """Validate exact nanosecond rounds; no interpolation or zero filling."""
        with self._lock:
            now = self._clock_ns()
            cutoff = max(self._epoch_ns, now - int(self.config.sample_max_age_s * 1e9))
            self._rounds = {stamp: values for stamp, values in self._rounds.items() if stamp >= cutoff}
            self._invalid_rounds = {stamp for stamp in self._invalid_rounds if stamp >= cutoff}
            if not isinstance(message, dict) or not isinstance(message.get("metrics"), list):
                self._reject(now, "Invalid process telemetry message.")
                return
            for metric in message["metrics"]:
                if not isinstance(metric, dict):
                    self._reject(now, "Invalid process telemetry metric.")
                    continue
                if metric.get("name") != self.config.measurement_name:
                    continue
                tags = metric.get("tags")
                if not isinstance(tags, dict):
                    self._reject(now, "Missing process data identity.")
                    continue
                if tags.get("device") != self.config.device_id:
                    continue
                stamp = metric.get("timestamp")
                if type(stamp) is not int:
                    self._reject(now, "Invalid process collection timestamp.")
                    continue
                if stamp < cutoff or stamp < self._sample_ns:
                    continue
                if stamp > now:
                    self._reject(now, "Process timestamp is ahead of the system clock.")
                    continue
                self._newest_ns = max(self._newest_ns, stamp)
                if stamp in self._invalid_rounds:
                    continue
                fields = metric.get("fields")
                names = [name for name in FIELDS if isinstance(fields, dict) and name in fields]
                if len(names) != 1:
                    self._reject(stamp, "Each process metric must contain exactly one expected value.")
                    continue
                name = names[0]
                quality = fields.get("Quality")
                if tags.get("id") != self.config.node_ids[name]:
                    self._reject(stamp, f"Unexpected OPC UA node for {name}.")
                    continue
                if not ((type(quality) is int and quality == 0)
                        or (isinstance(quality, str) and quality in GOOD_QUALITY_STRINGS)):
                    self._reject(stamp, f"OPC UA quality is not Good for {name}.")
                    continue
                value = fields[name]
                try:
                    valid = not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)
                except OverflowError:
                    valid = False
                if not valid:
                    self._reject(stamp, f"Invalid numeric value for {name}.")
                    continue
                value = float(value)
                if ((name in ("c", "count_middle") and value < 0)
                        or (name in ("T", "T_j", "T_j_set") and value <= -CELSIUS_TO_KELVIN)):
                    self._reject(stamp, f"Process value is outside its valid range: {name}.")
                    continue
                values = self._rounds.setdefault(stamp, {})
                if name in values and values[name] != value:
                    self._reject(stamp, f"Conflicting process values for {name}.")
                    continue
                values[name] = value
                if len(values) == len(FIELDS) and stamp >= self._sample_ns:
                    self._sample = dict(values)
                    self._sample_ns = stamp
            self._rounds = dict(sorted(self._rounds.items())[-128:])
            self._invalid_rounds = set(sorted(self._invalid_rounds)[-128:])

    def status(self):
        with self._lock:
            age = (self._clock_ns() - self._sample_ns) / 1e9 if self._sample is not None else None
            problem = self._problem if self._problem_ns >= self._sample_ns else None
            if not self.enabled:
                state = "disabled"
            elif not self._connected:
                state = "disconnected"
            elif problem:
                state = "invalid"
            elif age is None:
                state = "waiting"
            elif not 0 <= age <= self.config.sample_max_age_s:
                state = "stale"
            else:
                state = "ready"
            values = dict(self._sample) if state == "ready" else {}
            collected_at = (datetime.fromtimestamp(self._sample_ns / 1e9, timezone.utc).isoformat()
                            if self._sample is not None else None)
            newest = self._rounds.get(self._newest_ns, {})
            return {
                "status": state, "ready": state == "ready", "connected": self._connected,
                "device_id": self.config.device_id,
                "sample_age_s": age, "sample_max_age_s": self.config.sample_max_age_s,
                "collected_at": collected_at, "values": values,
                "missing_fields": [] if state == "ready" else [name for name in FIELDS if name not in newest],
                "error": self._connection_error if not self._connected else problem,
                "read_only": True,
            }
