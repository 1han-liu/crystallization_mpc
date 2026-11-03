"""Safe process-I/O boundary for the real crystallization equipment."""

from __future__ import annotations

import math
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Callable, Mapping

from crystallization_mpc.messaging.schema import utc_ts
from crystallization_mpc.infra.numeric import decode_numeric

CELSIUS_TO_KELVIN = 273.15


class ProcessAdapterError(RuntimeError):
    """Base error for process communication and safety failures."""


class ProcessConnectionError(ProcessAdapterError):
    """The process communication channel could not supply a valid result."""


class ControllerReadError(ProcessConnectionError):
    """A read attempt failed; the GUI-style loop warns and tries its next cycle.

    This is not a device-write receipt, a completed measurement, or permission
    to substitute historical values. ``stage`` identifies the failing read.
    """

    def __init__(self, message: str, *, stage: str = "process_read") -> None:
        self.stage = stage
        self.partial_process_values: dict[str, float] = {}
        super().__init__(message)


class ProcessSafetyError(ProcessAdapterError):
    """A process value or requested setpoint violated a safety constraint."""


class ProcessMeasurementPending(ProcessSafetyError):
    """Legacy waiting vocabulary, retained for archives and older adapters.

    Current measurement reads do not emit this for negative concentration.

    This exception is never a write receipt: when raised by a write method it
    guarantees that the corresponding command has not been published.
    """

    reason = "negative_concentration"

    def __init__(self, *, read_at: str, raw_c: float, waiting_since: str) -> None:
        self.read_at = read_at
        self.raw_c = float(raw_c)
        self.waiting_since = waiting_since
        super().__init__("Fresh process concentration is negative; waiting for valid measurement data.")

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "read_at": self.read_at,
                "raw_c": self.raw_c, "waiting_since": self.waiting_since}


class ProcessDataTimeoutPending(ProcessMeasurementPending):
    """A connected process bus has no fresh complete measurement yet.

    No current concentration or read timestamp is invented. A last-valid time
    is historical metadata only and never permission to reuse that sample.
    """

    reason = "process_data_timeout"

    def __init__(self, *, waiting_since: str, last_valid_read_at: str | None = None) -> None:
        self.waiting_since = waiting_since
        self.last_valid_read_at = last_valid_read_at
        self.read_at = None
        self.raw_c = None
        ProcessSafetyError.__init__(self, "Waiting for fresh complete process measurement data.")

    def to_dict(self) -> dict[str, Any]:
        metadata = {"reason": self.reason, "waiting_since": self.waiting_since}
        if self.last_valid_read_at is not None:
            metadata["last_valid_read_at"] = self.last_valid_read_at
        return metadata


class ProcessTransportPending(ProcessMeasurementPending):
    """A temporarily unavailable bus; no new device command was submitted."""

    reason = "process_transport_wait"

    def __init__(self, *, waiting_since: str, detail: str = "Reconnecting to the process message bus.") -> None:
        self.waiting_since = waiting_since
        self.detail = detail
        self.read_at = None
        self.raw_c = None
        ProcessSafetyError.__init__(self, detail)

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "waiting_since": self.waiting_since, "detail": self.detail}


class ProcessWritePending(ProcessAdapterError):
    """A completed numerical cycle has an unresolved device-write outcome.

    This deliberately does not inherit ProcessMeasurementPending: the write may
    already have reached the device and must never be blindly repeated.
    """

    reason = "process_write_confirmation_pending"

    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        import copy

        self.snapshot = copy.deepcopy(dict(snapshot))
        super().__init__(str(snapshot.get("detail") or "Waiting to confirm the previous device command."))

    def to_dict(self) -> dict[str, Any]:
        import copy

        return copy.deepcopy(self.snapshot)


@dataclass(frozen=True)
class ProcessState:
    """One real-equipment snapshot using the Controller contract units.

    ``T``, ``T_j`` and ``T_j_set`` are kelvin. ``c`` and ``count_middle``
    retain the scalar units exposed by the equipment OPC UA server.
    """

    T: float
    T_j: float
    c: float
    count_middle: float
    T_j_set: float | None
    read_at: str
    source_timestamps: Mapping[str, str | None] = field(default_factory=dict)
    server_timestamps: Mapping[str, str | None] = field(default_factory=dict)
    status_codes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("T", "T_j", "c", "count_middle"):
            # A measurement is passed to the same numerical stage as MATLAB,
            # including negative values and IEEE NaN/Inf. Types remain scalar.
            object.__setattr__(self, name, _measurement_number(getattr(self, name), name))
        if self.T_j_set is not None:
            object.__setattr__(self, "T_j_set", _measurement_number(self.T_j_set, "T_j_set"))
        if not str(self.read_at).strip():
            raise ValueError("Process read_at is required.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "T": float(self.T),
            "T_j": float(self.T_j),
            "c": float(self.c),
            "count_middle": float(self.count_middle),
            "T_j_set": float(self.T_j_set) if self.T_j_set is not None else None,
            "read_at": self.read_at,
            "source_timestamps": dict(self.source_timestamps),
            "server_timestamps": dict(self.server_timestamps),
            "status_codes": dict(self.status_codes),
        }


@dataclass(frozen=True)
class ProcessWriteResult:
    setpoint_K: float
    setpoint_C: float
    written_at: str
    verified: bool
    readback_K: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "setpoint_K": float(self.setpoint_K),
            "setpoint_C": float(self.setpoint_C),
            "written_at": self.written_at,
            "verified": self.verified,
            "readback_K": (
                float(self.readback_K) if self.readback_K is not None else None
            ),
        }


class ProcessAdapter(ABC):
    """Read process state and write the only Controller actuator setpoint."""

    @abstractmethod
    def connect(self) -> None:
        """Connect the process communication channel."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the process communication channel."""

    def set_command_context(self, run_id: str, run_started_at: str, tick_seq: int) -> None:
        """Bind a subsequent write to its experiment and control cycle.

        Local adapters do not need transport identifiers. The RabbitMQ adapter
        requires this context and echoes it through the writer confirmation.
        """

    @abstractmethod
    def read_state(self) -> ProcessState:
        """Read one complete, validated equipment snapshot."""

    def read_initial_state(self) -> ProcessState:
        """Read activation values; adapters without a separate path stay compatible."""
        return self.read_state()

    @abstractmethod
    def write_jacket_setpoint(self, setpoint_K: float) -> ProcessWriteResult:
        """Safely write a jacket-temperature setpoint expressed in kelvin."""

    @abstractmethod
    def status(self) -> Mapping[str, Any]:
        """Return connection, read, write and error status without credentials."""


class NoOpProcessAdapter(ProcessAdapter):
    """Safe default that never connects, reads, or writes equipment."""

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def read_state(self) -> ProcessState:
        raise ProcessConnectionError("The process communication adapter is disabled.")

    def write_jacket_setpoint(self, setpoint_K: float) -> ProcessWriteResult:
        raise ProcessConnectionError("The process communication adapter is disabled.")

    def status(self) -> Mapping[str, Any]:
        return {
            "class": f"{type(self).__module__}.{type(self).__qualname__}",
            "safe_noop": True,
            "connected": False,
            "read_count": 0,
            "write_count": 0,
            "reconnect_count": 0,
            "last_read_at": None,
            "last_write_at": None,
            "last_error": None,
        }


@dataclass(frozen=True)
class OpcUaNodeConfig:
    namespace_index: int = 2
    T_j_set: str = "iCache.Temperature1"
    T: str = "Local.iControl.Experiment1.Trends.Tr.Value"
    T_j: str = "Local.iControl.Experiment1.Trends.Tj.Value"
    c: str = "Local.iControl.Experiment1.Trends.load.Value"
    count_middle: str = "Local.iControl.Experiment1.Trends.count.Value"

    def __post_init__(self) -> None:
        if (
            isinstance(self.namespace_index, bool)
            or not isinstance(self.namespace_index, int)
            or self.namespace_index < 0
        ):
            raise ValueError("OPC UA namespace_index must be a non-negative integer.")
        for name in ("T_j_set", "T", "T_j", "c", "count_middle"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"OPC UA node {name} is required.")

    def node_ids(self) -> dict[str, str]:
        return {
            name: f"ns={self.namespace_index};s={getattr(self, name)}"
            for name in ("T_j_set", "T", "T_j", "c", "count_middle")
        }


@dataclass(frozen=True)
class OpcUaProcessConfig:
    endpoint: str = "opc.tcp://host.docker.internal:62552"
    nodes: OpcUaNodeConfig = field(default_factory=OpcUaNodeConfig)
    timeout_s: float = 5.0
    jacket_min_K: float = 253.15
    jacket_max_K: float = 453.15
    verify_write: bool = True
    verify_tolerance_K: float = 0.05

    def __post_init__(self) -> None:
        if not str(self.endpoint).strip():
            raise ValueError("OPC UA endpoint is required.")
        _positive_number(self.timeout_s, "OPC UA timeout_s")
        _finite_number(self.jacket_min_K, "jacket_min_K")
        _finite_number(self.jacket_max_K, "jacket_max_K")
        if self.jacket_min_K >= self.jacket_max_K:
            raise ValueError("jacket_min_K must be below jacket_max_K.")
        if not isinstance(self.verify_write, bool):
            raise ValueError("verify_write must be a boolean.")
        _positive_number(self.verify_tolerance_K, "verify_tolerance_K")


ClientFactory = Callable[[str, float], Any]


class OpcUaProcessAdapter(ProcessAdapter):
    """Synchronous OPC UA client for the port-62552 equipment nodes."""

    def __init__(
        self,
        config: OpcUaProcessConfig,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or _default_client_factory
        self._client: Any | None = None
        self._nodes: dict[str, Any] = {}
        self._setpoint_variant_type: Any | None = None
        self._connected = False
        self._lock = threading.RLock()
        self.read_count = 0
        self.write_count = 0
        self.reconnect_count = 0
        self.last_read_at: str | None = None
        self.last_write_at: str | None = None
        self.last_error: str | None = None
        self.last_state: ProcessState | None = None
        self.last_write: ProcessWriteResult | None = None
        self._last_field_values: dict[str, float] = {}
        self._initial_setpoint_reading: dict[str, Any] | None = None

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            client = self._client_factory(self.config.endpoint, self.config.timeout_s)
            try:
                client.connect()
                nodes = {
                    name: client.get_node(node_id)
                    for name, node_id in self.config.nodes.node_ids().items()
                }
                variant_type = nodes["T_j_set"].read_data_type_as_variant_type()
            except Exception as exc:
                try:
                    client.disconnect()
                except Exception:
                    pass
                self.last_error = str(exc)
                raise ProcessConnectionError(
                    f"Could not connect to OPC UA equipment endpoint: {exc}"
                ) from exc
            self._client = client
            self._nodes = nodes
            self._setpoint_variant_type = variant_type
            self._connected = True
            self.last_error = None

    def disconnect(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._nodes = {}
            self._setpoint_variant_type = None
            self._connected = False
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass

    def read_initial_state(self) -> ProcessState:
        with self._lock:
            # MATLAB clears its experiment lists before the direct initial
            # reads. A new initialization does not inherit empty-value fallback.
            self._last_field_values.clear()
            self._initial_setpoint_reading = None
            return self._read_process_state(initial=True)

    def read_state(self) -> ProcessState:
        return self._read_process_state(initial=False)

    def _read_process_state(self, *, initial: bool) -> ProcessState:
        with self._lock:
            try:
                # MATLAB checks connection status before reading. It does not
                # retry the failed read within the same control iteration.
                if not self._connected:
                    if self._client is not None:
                        self.reconnect_count += 1
                    self.connect()
                state = self._read_state_connected(initial=initial)
            except ProcessSafetyError as exc:
                self.last_error = str(exc)
                raise
            except ControllerReadError as exc:
                self.last_error = str(exc)
                raise
            except Exception as exc:
                self.last_error = str(exc)
                # A timeout alone does not establish that a session closed.
                # An explicit connection failure is observed by the next read.
                if isinstance(exc, (ConnectionError, EOFError)):
                    self._connected = False
                raise ControllerReadError(f"OPC UA process read failed: {exc}") from exc
            self.read_count += 1
            self.last_read_at = state.read_at
            self.last_state = state
            self.last_error = None
            return state

    def write_jacket_setpoint(self, setpoint_K: float) -> ProcessWriteResult:
        value_K = _finite_number(setpoint_K, "T_j_set")
        if not self.config.jacket_min_K <= value_K <= self.config.jacket_max_K:
            raise ProcessSafetyError(
                "T_j_set is outside the configured safe jacket-temperature range "
                f"[{self.config.jacket_min_K}, {self.config.jacket_max_K}] K."
            )
        with self._lock:
            try:
                result = self._with_one_reconnect(
                    lambda: self._write_setpoint_connected(value_K),
                    "write",
                )
            except Exception as exc:
                self.last_error = str(exc)
                raise
            self.write_count += 1
            self.last_write_at = result.written_at
            self.last_write = result
            self.last_error = None
            return result

    def _with_one_reconnect(self, operation: Callable[[], Any], name: str) -> Any:
        errors: list[Exception] = []
        for attempt in range(2):
            try:
                self.connect()
                return operation()
            except ProcessSafetyError:
                raise
            except Exception as exc:
                errors.append(exc)
                self.disconnect()
                if attempt == 0:
                    self.reconnect_count += 1
        raise ProcessConnectionError(
            f"OPC UA {name} failed after reconnect: {errors[-1]}"
        ) from errors[-1]

    def _read_state_connected(self, *, initial: bool) -> ProcessState:
        readings: dict[str, Any] = {}
        partial: dict[str, float] = {}
        names = (("T", "T_j", "T_j_set", "c", "count_middle") if initial else
                 ("T", "T_j", "c", "count_middle"))
        for name in names:
            try:
                readings[name] = self._read_node(name, measurement=True, empty_fallback=not initial)
            except ProcessSafetyError:
                raise
            except Exception as exc:
                error = exc if isinstance(exc, ControllerReadError) else ControllerReadError(str(exc))
                error.partial_process_values = dict(partial)
                if isinstance(exc, (ConnectionError, EOFError)):
                    self._connected = False
                if error is exc:
                    raise
                raise error from exc
            partial[name] = float(readings[name]["value"]) + (
                CELSIUS_TO_KELVIN if name in ("T", "T_j", "T_j_set") else 0.0
            )
            if initial and name == "T_j_set":
                # Source assignment has already happened even if a later
                # initial c/count read throws and initialization cannot finish.
                self._initial_setpoint_reading = dict(readings[name])
        if not initial and self._initial_setpoint_reading is not None:
            # The GUI runtime measurement reads only four fields. Preserve the
            # actual initial setpoint and its provenance without another read.
            readings["T_j_set"] = dict(self._initial_setpoint_reading)
        return ProcessState(
            T=readings["T"]["value"] + CELSIUS_TO_KELVIN,
            T_j=readings["T_j"]["value"] + CELSIUS_TO_KELVIN,
            c=readings["c"]["value"],
            count_middle=readings["count_middle"]["value"],
            T_j_set=(readings["T_j_set"]["value"] + CELSIUS_TO_KELVIN
                     if "T_j_set" in readings else None),
            read_at=utc_ts(),
            source_timestamps={
                name: values["source_timestamp"]
                for name, values in readings.items()
            },
            server_timestamps={
                name: values["server_timestamp"]
                for name, values in readings.items()
            },
            status_codes={
                name: values["status_code"] for name, values in readings.items()
            },
        )

    def _read_node(self, name: str, *, measurement: bool = False,
                   empty_fallback: bool = True) -> dict[str, Any]:
        data_value = self._nodes[name].read_data_value()
        status = data_value.StatusCode
        if not status.is_good():
            raise ProcessConnectionError(
                f"OPC UA node {name} returned bad status {status}."
            )
        raw = None if data_value.Value is None else data_value.Value.Value
        empty = raw is None or (isinstance(raw, (list, tuple)) and not raw)
        if empty and empty_fallback and name in ("c", "count_middle"):
            if name not in self._last_field_values:
                raise ControllerReadError(f"OPC UA node {name} returned empty before any prior value.")
            value = self._last_field_values[name]
        elif empty:
            raise ControllerReadError(f"OPC UA node {name} returned no value.")
        else:
            value = _measurement_number(raw, name) if measurement else _finite_number(raw, name)
            if empty_fallback and name in ("c", "count_middle"):
                # Initial readValue results are scalar workspace variables;
                # initialize_lists leaves the runtime c/count lists empty.
                # Only a successfully executed runtime read supplies list(end).
                self._last_field_values[name] = value
        return {
            "value": value,
            "status_code": str(status),
            "source_timestamp": _timestamp_text(data_value.SourceTimestamp),
            "server_timestamp": _timestamp_text(data_value.ServerTimestamp),
        }

    def _write_setpoint_connected(self, setpoint_K: float) -> ProcessWriteResult:
        raw_C = setpoint_K - CELSIUS_TO_KELVIN
        node = self._nodes["T_j_set"]
        if self._setpoint_variant_type is None:
            node.write_value(float(raw_C))
        else:
            node.write_value(float(raw_C), self._setpoint_variant_type)

        readback_K: float | None = None
        verified = False
        if self.config.verify_write:
            readback = self._read_node("T_j_set")
            readback_K = readback["value"] + CELSIUS_TO_KELVIN
            if abs(readback_K - setpoint_K) > self.config.verify_tolerance_K:
                raise ProcessSafetyError(
                    "OPC UA T_j_set readback does not match the requested setpoint."
                )
            verified = True
        return ProcessWriteResult(
            setpoint_K=setpoint_K,
            setpoint_C=raw_C,
            written_at=utc_ts(),
            verified=verified,
            readback_K=readback_K,
        )

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "class": f"{type(self).__module__}.{type(self).__qualname__}",
                "safe_noop": False,
                "endpoint": self.config.endpoint,
                "node_ids": self.config.nodes.node_ids(),
                "connected": self._connected,
                "read_count": self.read_count,
                "write_count": self.write_count,
                "reconnect_count": self.reconnect_count,
                "last_read_at": self.last_read_at,
                "last_write_at": self.last_write_at,
                "last_error": self.last_error,
                "last_state": (
                    self.last_state.to_dict() if self.last_state is not None else None
                ),
                "last_write": (
                    self.last_write.to_dict() if self.last_write is not None else None
                ),
                "safe_jacket_range_K": [
                    self.config.jacket_min_K,
                    self.config.jacket_max_K,
                ],
                "write_verification": self.config.verify_write,
            }


def _default_client_factory(endpoint: str, timeout_s: float) -> Any:
    try:
        from asyncua.sync import Client
    except ImportError as exc:  # pragma: no cover - exercised in runtime image
        raise RuntimeError(
            "asyncua is required by the OPC UA writer."
        ) from exc
    return Client(endpoint, timeout=timeout_s)


def _measurement_number(value: Any, name: str) -> float:
    try:
        return decode_numeric(value, name)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProcessSafetyError(f"{name} must be a numeric measurement scalar.") from exc


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ProcessSafetyError(f"{name} must be a real number.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ProcessSafetyError(f"{name} must be finite.")
    return normalized


def _positive_number(value: Any, name: str) -> float:
    normalized = _finite_number(value, name)
    if normalized <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return normalized


def _timestamp_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        timestamp = value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


__all__ = [
    "CELSIUS_TO_KELVIN",
    "NoOpProcessAdapter",
    "OpcUaNodeConfig",
    "OpcUaProcessAdapter",
    "OpcUaProcessConfig",
    "ProcessAdapter",
    "ProcessAdapterError",
    "ProcessConnectionError",
    "ControllerReadError",
    "ProcessSafetyError",
    "ProcessMeasurementPending",
    "ProcessDataTimeoutPending",
    "ProcessState",
    "ProcessWriteResult",
]
