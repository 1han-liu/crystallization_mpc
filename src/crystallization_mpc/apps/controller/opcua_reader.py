"""Synchronous, read-only OPC UA calls at the Controller's source read points.

Each field read makes one actual Read request. Neither old values nor failed
requests are cached, and this client never sends device writes.
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from crystallization_mpc.messaging.schema import utc_ts

from .process import (
    ControllerReadError,
    MATLAB_EMPTY,
    ProcessConnectionError,
    is_matlab_empty,
)

FIELDS = ("T", "T_j", "c", "count_middle", "T_j_set")


@dataclass(frozen=True)
class OpcUaReadConfig:
    endpoint: str = field(default="opc.tcp://host.docker.internal:62552", repr=False)
    timeout_s: float = 5.0
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    auth_method: str = "Anonymous"
    security_policy: str = "None"
    security_mode: str = "None"
    certificate: str = ""
    private_key: str = field(default="", repr=False)
    server_certificate: str = ""
    server_uri: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError("OPCUA_ENDPOINT is required.")
        # asyncua's sync constructor starts a thread before parsing the URL.
        # Reject malformed endpoints here, while no client resources exist.
        try:
            endpoint = urlsplit(self.endpoint)
            valid_endpoint = (endpoint.scheme == "opc.tcp" and bool(endpoint.hostname)
                              and endpoint.port is not None and endpoint.port > 0)
        except ValueError:
            valid_endpoint = False
        if not valid_endpoint:
            raise ValueError("OPCUA_ENDPOINT must be an opc.tcp URL with a host and port.")
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)):
            raise ValueError("OPCUA_TIMEOUT_S must be a positive finite number.")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("OPCUA_TIMEOUT_S must be a positive finite number.")
        if self.security_policy == "None":
            if self.security_mode != "None":
                raise ValueError("OPCUA_SECURITY_MODE must be None with policy None.")
        else:
            if self.security_mode not in {"Sign", "SignAndEncrypt"}:
                raise ValueError("OPCUA_SECURITY_MODE must be Sign or SignAndEncrypt.")
            if not self.certificate or not self.private_key:
                raise ValueError("Secure OPC UA requires a client certificate and private key.")
        if self.auth_method not in {"Anonymous", "UserName", "Certificate"}:
            raise ValueError("OPCUA_AUTH_METHOD must be Anonymous, UserName or Certificate.")
        if self.password and not self.username:
            raise ValueError("OPCUA_PASSWORD requires OPCUA_USERNAME.")
        if self.auth_method == "UserName" and not self.username:
            raise ValueError("UserName authentication requires OPCUA_USERNAME.")
        if self.auth_method == "Anonymous" and (self.username or self.password):
            raise ValueError("Set OPCUA_AUTH_METHOD=UserName to use username credentials.")
        if self.auth_method == "Certificate" and (not self.certificate or not self.private_key):
            raise ValueError("Certificate authentication requires a certificate and private key.")

    @classmethod
    def from_env(cls) -> "OpcUaReadConfig":
        return cls(
            endpoint=os.getenv("OPCUA_ENDPOINT", "opc.tcp://host.docker.internal:62552"),
            timeout_s=float(os.getenv("OPCUA_TIMEOUT_S", "5")),
            username=os.getenv("OPCUA_USERNAME", ""),
            password=os.getenv("OPCUA_PASSWORD", ""),
            auth_method=os.getenv("OPCUA_AUTH_METHOD", "UserName" if os.getenv("OPCUA_USERNAME") else "Anonymous"),
            security_policy=os.getenv("OPCUA_SECURITY_POLICY", "None"),
            security_mode=os.getenv("OPCUA_SECURITY_MODE", "None"),
            certificate=os.getenv("OPCUA_CERTIFICATE", ""),
            private_key=os.getenv("OPCUA_PRIVATE_KEY", ""),
            server_certificate=os.getenv("OPCUA_SERVER_CERTIFICATE", ""),
            server_uri=os.getenv("OPCUA_SERVER_URI", "").strip() or None,
        )


def _create_client(config: OpcUaReadConfig) -> Any:
    from asyncua.sync import Client

    client = Client(config.endpoint, timeout=config.timeout_s,
                    sync_wrapper_timeout=config.timeout_s + 1)
    try:
        # The sync wrapper does not proxy server_uri to CreateSession.
        client.aio_obj.server_uri = config.server_uri
        if config.auth_method == "UserName":
            client.set_user(config.username)
            client.set_password(config.password)
        elif config.auth_method == "Certificate":
            client.load_client_certificate(config.certificate)
            client.load_private_key(config.private_key)
        if config.security_policy != "None":
            security = [config.security_policy, config.security_mode,
                        config.certificate, config.private_key]
            if config.server_certificate:
                security.append(config.server_certificate)
            client.set_security_string(",".join(security))
    except Exception:
        try:
            client.disconnect()
        except Exception:
            pass
        raise
    return client


class OpcUaReadClient:
    """Return raw node values without a numeric or timestamp admission policy."""

    def __init__(self, config: OpcUaReadConfig, node_ids: Mapping[str, str], *,
                 client_factory: Callable[[OpcUaReadConfig], Any] | None = None) -> None:
        if set(node_ids) != set(FIELDS) or any(
            not isinstance(node_ids[name], str) or not node_ids[name].strip()
            for name in FIELDS
        ):
            raise ValueError("OPC UA reader requires the five complete process NodeIds.")
        self.config = config
        self._node_ids = dict(node_ids)
        self._client_factory = client_factory or _create_client
        self._lock = threading.RLock()
        self._client: Any | None = None
        self._nodes: dict[str, Any] = {}
        self._connected = False
        self._connected_once = False
        self.read_count = 0
        self.read_attempt_count = 0
        self.reconnect_count = 0
        self.last_read_at: str | None = None
        self.last_error: str | None = None

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                self._discard_if_disconnected()
                if self._connected:
                    return
            client = None
            try:
                client = self._client_factory(self.config)
                client.connect()
                nodes = {name: client.get_node(node_id)
                         for name, node_id in self._node_ids.items()}
            except Exception as exc:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                self.last_error = f"OPC UA connection failed ({type(exc).__name__})."
                raise ProcessConnectionError(self.last_error) from None
            self._client = client
            self._nodes = nodes
            self._connected = True
            if self._connected_once:
                self.reconnect_count += 1
            self._connected_once = True
            self.last_error = None

    def disconnect(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._nodes = {}
            self._connected = False
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass

    def _discard_if_disconnected(self) -> None:
        """Inspect library background state, without an extra device Read."""
        if self._client is None:
            return
        check = getattr(self._client, "check_connection", None)
        if check is not None:
            try:
                check()
            except Exception:
                self.disconnect()

    def read_initial_field(self, name: str) -> Any:
        return self._read_field(name)

    def read_process_field(self, name: str) -> Any:
        try:
            return self._read_field(name)
        except ControllerReadError:
            if name in {"T", "T_j"}:
                error = ControllerReadError('Output argument "value" not assigned a value in read_value.')
                error.reference_identifier = "MATLAB:unassignedOutputs"
                raise error from None
            raise

    def _read_field(self, name: str) -> Any:
        if name not in self._node_ids:
            raise ControllerReadError("Unknown OPC UA process field.")
        with self._lock:
            try:
                self.connect()
            except ProcessConnectionError:
                raise ControllerReadError(self.last_error or "OPC UA connection failed.") from None
            error_message = None
            try:
                self.read_attempt_count += 1
                data_value = self._nodes[name].read_data_value()
                if not data_value.StatusCode.is_good():
                    code = getattr(data_value.StatusCode, "value", None)
                    detail = f"0x{code:08X}" if type(code) is int else "non-Good"
                    error_message = f"OPC UA node {name} returned status {detail}."
                    raise ControllerReadError(error_message)
                value = None if data_value.Value is None else data_value.Value.Value
            except Exception as exc:
                self.last_error = error_message or f"OPC UA read for {name} failed ({type(exc).__name__})."
                # A node quality failure is not evidence that its session died.
                # If the library also detects a lost connection, reconnect only
                # on the next call; never replay this failed Read.
                self._discard_if_disconnected()
                raise ControllerReadError(self.last_error) from None
            self.read_count += 1
            self.last_read_at = utc_ts()
            self.last_error = None
            return MATLAB_EMPTY if value is None or is_matlab_empty(value) else value

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "read_transport": "opcua",
                "connected": self._connected,
                "read_count": self.read_count,
                "read_attempt_count": self.read_attempt_count,
                "reconnect_count": self.reconnect_count,
                "last_read_at": self.last_read_at,
                "last_error": self.last_error,
                "node_ids": dict(self._node_ids),
            }
