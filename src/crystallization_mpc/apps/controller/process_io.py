"""Controller reads OPC UA directly; device writes keep the existing Writer path."""
from __future__ import annotations

from typing import Any, Mapping

from crystallization_mpc.messaging.schema import utc_ts

from .opcua_reader import OpcUaReadClient
from .process import (
    CELSIUS_TO_KELVIN, ProcessAdapter, ProcessState, ProcessWriteError,
    ProcessWriteResult,
)


class ControllerProcessAdapter(ProcessAdapter):
    """A successful read is an actual OPC UA Read, never a reused delivery.

    The message bus is needed only when a calculated setpoint is submitted.
    Its failure cannot prevent the preceding source reads and calculations.
    This class has no OPC UA write client or write method of its own.
    """

    def __init__(self, reader: OpcUaReadClient, writer: ProcessAdapter) -> None:
        self.reader = reader
        self.writer = writer

    def connect(self) -> None:
        self.reader.connect()

    def disconnect(self) -> None:
        try:
            self.reader.disconnect()
        finally:
            self.writer.disconnect()

    def read_initial_field(self, name: str) -> Any:
        return self.reader.read_initial_field(name)

    def read_process_field(self, name: str) -> Any:
        return self.reader.read_process_field(name)

    def read_state(self) -> ProcessState:
        # Compatibility for aggregate callers. The Controller uses the five
        # initial / four runtime read positions individually, in source order.
        values = {name: self.read_initial_field(name)
                  for name in ("T", "T_j", "T_j_set", "c", "count_middle")}
        return ProcessState(
            T=values["T"] + CELSIUS_TO_KELVIN,
            T_j=values["T_j"] + CELSIUS_TO_KELVIN,
            T_j_set=values["T_j_set"] + CELSIUS_TO_KELVIN,
            c=values["c"], count_middle=values["count_middle"], read_at=utc_ts(),
        )

    def set_command_context(self, run_id: str, run_started_at: str, tick_seq: int) -> None:
        self.writer.set_command_context(run_id, run_started_at, tick_seq)

    def write_jacket_setpoint(self, setpoint_K: float) -> ProcessWriteResult:
        try:
            self.writer.connect()
        except Exception as exc:
            # No command was handed to Writer. Do not misreport this as an
            # attempted/unknown physical write, and never retry a command here.
            raise ProcessWriteError(
                "Writer message bus connection failed before command submission.",
                command_sent=False, write_outcome="failed",
            ) from exc
        return self.writer.write_jacket_setpoint(setpoint_K)

    def status(self) -> Mapping[str, Any]:
        read = dict(self.reader.status())
        write = dict(self.writer.status())
        return {
            **write,
            "class": f"{type(self).__module__}.{type(self).__qualname__}",
            "safe_noop": False,
            "transport": "opcua-read/rabbitmq-write",
            "read_transport": "opcua",
            "write_transport": "rabbitmq",
            "connected": read.get("connected", False),
            "write_connected": write.get("connected", False),
            "read_count": read.get("read_count", 0),
            "last_read_at": read.get("last_read_at"),
            "reader": read,
            "writer": write,
            "limitations": [
                "Device writes retain the external Writer validation and command expiry.",
                "Live G input retains its existing message delivery path.",
            ],
        }
