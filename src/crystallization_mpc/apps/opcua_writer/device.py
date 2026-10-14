"""One attempted write, using the existing conversion and readback implementation."""

from __future__ import annotations

from crystallization_mpc.apps.controller.process import (
    OpcUaProcessAdapter,
    ProcessConnectionError,
    ProcessSafetyError,
    ProcessWriteResult,
    _finite_number,
)

from .config import WriterSettings


class SetpointDevice(OpcUaProcessAdapter):
    """Resolve only the allowlisted node; never replay a write after reconnect.

    The old process adapter's conversion, VariantType and readback code remains
    the single implementation. Its retry wrapper is deliberately not used here:
    a network failure after Write was sent does not prove that Write failed.
    """

    def __init__(self, settings: WriterSettings, *, client_factory=None) -> None:
        super().__init__(settings.process, client_factory=client_factory or settings.create_client)

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            client = None
            try:
                client = self._client_factory(self.config.endpoint, self.config.timeout_s)
                client.connect()
                node = client.get_node(self.config.nodes.node_ids()["T_j_set"])
                variant_type = node.read_data_type_as_variant_type()
            except Exception as exc:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                raise ProcessConnectionError(f"OPC UA connection failed: {exc}") from exc
            self._client = client
            self._nodes = {"T_j_set": node}
            self._setpoint_variant_type = variant_type
            self._connected = True

    def write_jacket_setpoint(self, setpoint_K: float) -> ProcessWriteResult:
        value = _finite_number(setpoint_K, "T_j_set")
        if not self.config.jacket_min_K <= value <= self.config.jacket_max_K:
            raise ProcessSafetyError("T_j_set is outside the configured safe range.")
        with self._lock:
            if not self._connected:
                raise ProcessConnectionError("Connect before attempting a setpoint write.")
            try:
                result = self._write_setpoint_connected(value)
            except Exception as exc:
                self.last_error = str(exc)
                self.disconnect()
                raise
            self.write_count += 1
            self.last_write = result
            self.last_write_at = result.written_at
            self.last_error = None
            return result
