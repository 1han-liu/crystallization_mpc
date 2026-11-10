"""Read-only observation of each field in Central's own telemetry subscription."""
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
        self._connection_error = None
        self._message_error = None
        self._fields = {}
        self._last_received_ns = None

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

    def _run(self):
        while not self._stop.is_set():
            connection = None
            try:
                connection = self._connection_factory(self.config.rabbit_url)
                channel = connection.channel()
                channel.exchange_declare(exchange=self.config.exchange, exchange_type="topic", durable=True)
                declared = channel.queue_declare(
                    queue="", exclusive=True, auto_delete=True, arguments={"x-max-length": 1000},
                )
                channel.queue_bind(exchange=self.config.exchange, queue=declared.method.queue,
                                   routing_key=TELEMETRY_ROUTE)
                channel.basic_consume(queue=declared.method.queue, on_message_callback=self._on_message,
                                      auto_ack=True)
                self._set_connection(True)
                while not self._stop.is_set() and connection.is_open:
                    connection.process_data_events(time_limit=0.25)
            except Exception:
                # Do not expose connection strings or credentials in UI errors.
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

    def accept(self, message):
        """Keep each latest observation, including a missing value or bad quality.

        Unlike Controller's calculation cache, this monitor displays the latest
        raw observation and its error. Timestamps describe that observation;
        they never decide whether another field may be displayed.
        """
        with self._lock:
            if not isinstance(message, dict) or not isinstance(message.get("metrics"), list):
                self._message_error = "Invalid process telemetry message."
                return
            now = self._clock_ns()
            for metric in message["metrics"]:
                if not isinstance(metric, dict):
                    self._message_error = "Invalid process telemetry metric."
                    continue
                if metric.get("name") != self.config.measurement_name:
                    continue
                tags, values = metric.get("tags"), metric.get("fields")
                if not isinstance(tags, dict) or tags.get("device") != self.config.device_id:
                    continue
                if not isinstance(values, dict):
                    self._message_error = "Missing process telemetry fields."
                    continue
                names = [name for name in FIELDS
                         if name in values or tags.get("id") == self.config.node_ids[name]]
                if len(names) != 1:
                    self._message_error = "Each process metric must identify one expected field."
                    continue
                name = names[0]
                quality, value = values.get("Quality"), values.get(name)
                try:
                    numeric = not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)
                    value = float(value) if numeric else None
                except (OverflowError, ValueError):
                    value = None
                error = None
                if tags.get("id") != self.config.node_ids[name]:
                    error = "Unexpected OPC UA node."
                elif name not in values:
                    error = "Telegraf omitted the numeric value."
                elif value is None:
                    error = "Non-finite or missing numeric value."
                elif not ((type(quality) is int and quality == 0)
                          or (isinstance(quality, str) and quality in GOOD_QUALITY_STRINGS)):
                    error = f"OPC UA quality is not Good: {quality}."
                elif name == "c" and value < 0:
                    error = "Negative concentration."
                elif name == "count_middle" and value < 0:
                    error = "Negative process count."
                elif name.startswith("T") and value <= -CELSIUS_TO_KELVIN:
                    error = "Temperature is below its physical range."
                stamp = metric.get("timestamp")
                stamp = stamp if type(stamp) is int and 0 < stamp <= 2**63 - 1 else None
                basis = tags.get("timestamp_basis")
                basis = "gather" if basis in (None, "", "gather") else basis
                self._fields[name] = {
                    "value": value, "quality": quality, "error": error,
                    "updated_at": self._timestamp_text(stamp),
                    "timestamp_basis": basis if basis in ("source", "gather") else "unknown",
                    "received_at": self._timestamp_text(now), "timestamp_ns": stamp,
                }
                self._last_received_ns = now
                self._message_error = None

    @staticmethod
    def _timestamp_text(stamp):
        if type(stamp) is not int:
            return None
        try:
            seconds, nanos = divmod(stamp, 1_000_000_000)
            return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"
        except (ValueError, OverflowError, OSError):
            return None

    def status(self):
        """Return immediately, even before the first field has arrived."""
        with self._lock:
            now = self._clock_ns()
            fields = {}
            for name in FIELDS:
                entry = dict(self._fields.get(name, {
                    "value": None, "quality": None, "error": None, "updated_at": None,
                    "received_at": None, "timestamp_basis": "unknown",
                }))
                stamp = entry.pop("timestamp_ns", None)
                entry["age_s"] = (now - stamp) / 1e9 if stamp is not None else None
                fields[name] = entry
            missing = [name for name, entry in fields.items() if entry["value"] is None]
            if not self.enabled:
                state = "disabled"
            elif not self._connected:
                state = "disconnected"
            elif self._message_error or any(entry["error"] for entry in fields.values()):
                state = "invalid"
            elif not self._fields:
                state = "waiting"
            elif missing:
                state = "partial"
            else:
                state = "ready"
            return {
                "status": state, "connected": self._connected, "device_id": self.config.device_id,
                "fields": fields, "missing_fields": missing,
                "last_received_at": self._timestamp_text(self._last_received_ns),
                "error": self._connection_error if not self._connected else self._message_error,
                "read_only": True,
            }
