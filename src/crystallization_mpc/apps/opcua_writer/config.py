"""Device credentials belong to the writer and collector, never Controller."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from crystallization_mpc.apps.controller.process import (
    OpcUaNodeConfig,
    OpcUaProcessConfig,
)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


@dataclass(frozen=True)
class WriterSettings:
    rabbit_url: str = field(default="amqp://guest:guest@localhost:5672/%2F", repr=False)
    rabbit_exchange: str = "idp.bus"
    rabbit_queue: str = "opcua_writer.in"
    journal_path: str = "/data/commands.sqlite3"
    process: OpcUaProcessConfig = field(default_factory=OpcUaProcessConfig)
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    auth_method: str = "Anonymous"
    security_policy: str = "None"
    security_mode: str = "None"
    certificate: str = ""
    private_key: str = field(default="", repr=False)
    server_certificate: str = ""

    def __post_init__(self) -> None:
        if not self.rabbit_exchange.strip() or not self.rabbit_queue.strip():
            raise ValueError("Writer exchange and queue are required.")
        if not self.journal_path.strip():
            raise ValueError("A durable writer journal path is required.")
        if not self.process.verify_write:
            raise ValueError("OPCUA_VERIFY_WRITE must be true: the process bus requires verified device readback.")
        if self.security_policy == "None":
            if self.security_mode != "None":
                raise ValueError("OPCUA_SECURITY_MODE must be None with policy None.")
        else:
            if self.security_mode not in {"Sign", "SignAndEncrypt"}:
                raise ValueError("OPCUA_SECURITY_MODE must be Sign or SignAndEncrypt.")
            if not self.certificate or not self.private_key:
                raise ValueError("Secure OPC UA requires a client certificate and private key.")
        if self.password and not self.username:
            raise ValueError("OPCUA_PASSWORD requires OPCUA_USERNAME.")
        if self.auth_method not in {"Anonymous", "UserName", "Certificate"}:
            raise ValueError("OPCUA_AUTH_METHOD must be Anonymous, UserName or Certificate.")
        if self.auth_method == "UserName" and not self.username:
            raise ValueError("UserName authentication requires OPCUA_USERNAME.")
        if self.auth_method == "Anonymous" and (self.username or self.password):
            raise ValueError("Set OPCUA_AUTH_METHOD=UserName to use username credentials.")
        if self.auth_method == "Certificate" and (not self.certificate or not self.private_key):
            raise ValueError("Certificate authentication requires a certificate and private key.")

    @classmethod
    def from_env(cls) -> "WriterSettings":
        defaults = OpcUaNodeConfig()
        nodes = OpcUaNodeConfig(
            namespace_index=int(os.getenv("OPCUA_NAMESPACE_INDEX", "2")),
            T_j_set=os.getenv("OPCUA_T_J_SET_NODE", defaults.T_j_set),
            T=os.getenv("OPCUA_T_NODE", defaults.T),
            T_j=os.getenv("OPCUA_T_J_NODE", defaults.T_j),
            c=os.getenv("OPCUA_C_NODE", defaults.c),
            count_middle=os.getenv("OPCUA_COUNT_MIDDLE_NODE", defaults.count_middle),
        )
        return cls(
            rabbit_url=os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F"),
            rabbit_exchange=os.getenv("RABBIT_EXCHANGE", "idp.bus"),
            rabbit_queue=os.getenv("OPCUA_WRITER_QUEUE", "opcua_writer.in"),
            journal_path=os.getenv("OPCUA_WRITER_JOURNAL", "/data/commands.sqlite3"),
            process=OpcUaProcessConfig(
                endpoint=os.getenv("OPCUA_ENDPOINT", "opc.tcp://host.docker.internal:62552"),
                nodes=nodes,
                timeout_s=float(os.getenv("OPCUA_TIMEOUT_S", "5")),
                jacket_min_K=float(os.getenv("OPCUA_JACKET_MIN_K", "253.15")),
                jacket_max_K=float(os.getenv("OPCUA_JACKET_MAX_K", "453.15")),
                verify_write=_bool("OPCUA_VERIFY_WRITE", True),
                verify_tolerance_K=float(os.getenv("OPCUA_VERIFY_TOLERANCE_K", "0.05")),
            ),
            username=os.getenv("OPCUA_USERNAME", ""),
            password=os.getenv("OPCUA_PASSWORD", ""),
            auth_method=os.getenv("OPCUA_AUTH_METHOD", "UserName" if os.getenv("OPCUA_USERNAME") else "Anonymous"),
            security_policy=os.getenv("OPCUA_SECURITY_POLICY", "None"),
            security_mode=os.getenv("OPCUA_SECURITY_MODE", "None"),
            certificate=os.getenv("OPCUA_CERTIFICATE", ""),
            private_key=os.getenv("OPCUA_PRIVATE_KEY", ""),
            server_certificate=os.getenv("OPCUA_SERVER_CERTIFICATE", ""),
        )

    def create_client(self, endpoint: str, timeout_s: float):
        from asyncua.sync import Client

        client = Client(endpoint, timeout=timeout_s, sync_wrapper_timeout=timeout_s + 1)
        try:
            if self.auth_method == "UserName":
                client.set_user(self.username)
                client.set_password(self.password)
            elif self.auth_method == "Certificate":
                client.load_client_certificate(self.certificate)
                client.load_private_key(self.private_key)
            if self.security_policy != "None":
                parts = [self.security_policy, self.security_mode, self.certificate, self.private_key]
                if self.server_certificate:
                    parts.append(self.server_certificate)
                client.set_security_string(",".join(parts))
        except Exception:
            try:
                client.disconnect()
            except Exception:
                pass
            raise
        return client
