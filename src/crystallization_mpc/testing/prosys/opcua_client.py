"""Allowlisted client for a real Prosys Simulation Server (never an OPC UA server).

Product identification is a commissioning guard, not cryptographic identity.
Anonymous/None is permitted only on the isolated test network. No write is sent
until BuildInfo, node types, quality and user write permissions are checked.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from asyncua import ua


DEFAULT_IDENTIFIERS = {
    "T": "Local.iControl.Experiment1.Trends.Tr.Value",
    "T_j": "Local.iControl.Experiment1.Trends.Tj.Value",
    "c": "Local.iControl.Experiment1.Trends.load.Value",
    "count_middle": "Local.iControl.Experiment1.Trends.count.Value",
    "T_j_set": "iCache.Temperature1",
}
ENV_IDENTIFIERS = {
    "T": "OPCUA_T_NODE", "T_j": "OPCUA_T_J_NODE", "c": "OPCUA_C_NODE",
    "count_middle": "OPCUA_COUNT_MIDDLE_NODE", "T_j_set": "OPCUA_T_J_SET_NODE",
}
PROCESS_FIELDS = ("T", "T_j", "c", "count_middle")


@dataclass(frozen=True)
class ProsysConfig:
    endpoint: str
    namespace_index: int = 2
    identifiers: Mapping[str, str] | None = None
    timeout_s: float = 3.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "opc.tcp" or not parsed.hostname or parsed.hostname == "0.0.0.0":
            raise ValueError("An explicit, reachable opc.tcp Prosys test endpoint is required.")
        if self.namespace_index < 1 or not 0 < self.timeout_s <= 30:
            raise ValueError("Invalid namespace or timeout.")
        identifiers = dict(self.identifiers or DEFAULT_IDENTIFIERS)
        if set(identifiers) != set(DEFAULT_IDENTIFIERS) or any(
            not isinstance(value, str) or not value or value.startswith("ns=")
            for value in identifiers.values()
        ):
            raise ValueError("Exactly five nonempty string identifiers are required.")
        if len(set(identifiers.values())) != 5:
            raise ValueError("Process and setpoint nodes must be distinct.")
        object.__setattr__(self, "identifiers", identifiers)

    @classmethod
    def from_env(cls) -> "ProsysConfig":
        endpoint = os.environ.get("PROSYS_TEST_ENDPOINT") or os.environ.get("OPCUA_ENDPOINT")
        if not endpoint:
            raise ValueError("Set PROSYS_TEST_ENDPOINT explicitly; no real-device default is used.")
        return cls(
            endpoint=endpoint,
            namespace_index=int(os.getenv("OPCUA_NAMESPACE_INDEX", "2")),
            identifiers={key: os.getenv(ENV_IDENTIFIERS[key], default)
                         for key, default in DEFAULT_IDENTIFIERS.items()},
            timeout_s=float(os.getenv("PROSYS_TEST_OPCUA_TIMEOUT_S", "3")),
        )

    def node_ids(self) -> dict[str, str]:
        return {key: f"ns={self.namespace_index};s={identifier}"
                for key, identifier in self.identifiers.items()}


class ProsysVerificationError(RuntimeError):
    """No writes are authorized for an unverified or incompatible server."""


def _client_factory(endpoint: str, timeout_s: float) -> Any:
    from asyncua.sync import Client
    return Client(endpoint, timeout=timeout_s, sync_wrapper_timeout=timeout_s + 1)


class ProsysProcessClient:
    def __init__(self, config: ProsysConfig, *, client_factory: Callable = _client_factory):
        self.config = config
        self._factory = client_factory
        self._client: Any = None
        self._nodes: dict[str, Any] = {}
        self.server_identity: dict[str, Any] | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(self) -> dict[str, Any]:
        if self.connected:
            return dict(self.server_identity)
        # A failed new connection must never report the previous server as the
        # identity observed during this attempt.
        self.server_identity = None
        client = self._factory(self.config.endpoint, self.config.timeout_s)
        try:
            client.connect()
            build = client.get_node(ua.ObjectIds.Server_ServerStatus_BuildInfo).read_value()
            name = str(build.ProductName)
            manufacturer = str(build.ManufacturerName)
            product_uri = str(build.ProductUri)
            if not ("prosys" in (name + " " + manufacturer).casefold()
                    and "simulation" in name.casefold()):
                raise ProsysVerificationError("Endpoint is not identified as Prosys OPC UA Simulation Server.")
            namespace_array = list(client.get_node(ua.ObjectIds.Server_NamespaceArray).read_value())
            identity = {
                "endpoint": self.config.endpoint, "product_name": name,
                "manufacturer": manufacturer, "product_uri": product_uri,
                "software_version": str(build.SoftwareVersion),
                "build_number": str(build.BuildNumber),
                "server_uris": list(client.get_node(ua.ObjectIds.Server_ServerArray).read_value()),
                "namespace_uri": (namespace_array[self.config.namespace_index]
                                  if self.config.namespace_index < len(namespace_array) else None),
                "namespace_index": self.config.namespace_index,
                "node_ids": self.config.node_ids(),
                "product_identity_verified": True, "node_contract_verified": False,
            }
            # Preserve read-only product evidence when the five-node contract
            # fails. This does NOT set _client, connected, or authorize writes.
            self.server_identity = identity
            if self.config.namespace_index >= len(namespace_array):
                raise ProsysVerificationError("Configured namespace is absent.")
            nodes = {key: client.get_node(node_id)
                     for key, node_id in self.config.node_ids().items()}
            for key, node in nodes.items():
                if node.read_node_class() != ua.NodeClass.Variable:
                    raise ProsysVerificationError(f"{key} is not a Variable.")
                if node.read_data_type_as_variant_type() != ua.VariantType.Double:
                    raise ProsysVerificationError(f"{key} is not a Double.")
                self._read_good(node, key)
                if ua.AccessLevel.CurrentWrite not in node.get_user_access_level():
                    raise ProsysVerificationError(f"{key} must be writable in this test namespace.")
        except Exception:
            try:
                client.disconnect()
            except Exception:
                pass
            raise
        identity["node_contract_verified"] = True
        self._client, self._nodes, self.server_identity = client, nodes, identity
        return dict(identity)

    def close(self) -> None:
        client, self._client = self._client, None
        self._nodes = {}
        if client is not None:
            client.disconnect()

    @staticmethod
    def _read_good(node: Any, field: str) -> float:
        value = node.read_data_value()
        if not value.StatusCode.is_good() or value.Value.VariantType != ua.VariantType.Double:
            raise ProsysVerificationError(f"{field} quality/type is invalid.")
        number = value.Value.Value
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number):
            raise ProsysVerificationError(f"{field} is not a finite scalar.")
        return float(number)

    def read_values(self) -> dict[str, float]:
        if not self.connected:
            raise ProsysVerificationError("Connect and verify Prosys before reading.")
        return {key: self._read_good(node, key) for key, node in self._nodes.items()}

    def read_setpoint_K(self) -> float:
        if not self.connected:
            raise ProsysVerificationError("Connect and verify Prosys before reading.")
        value = self._read_good(self._nodes["T_j_set"], "T_j_set") + 273.15
        if not 253.15 <= value <= 453.15:
            raise ProsysVerificationError("Jacket setpoint is outside the approved test limits.")
        return value

    def write_process(self, values: Mapping[str, float]) -> None:
        if not self.connected:
            raise ProsysVerificationError("Connect and verify Prosys before writing.")
        if set(values) != set(PROCESS_FIELDS):
            raise ValueError("Plant writes exactly T, T_j, c, count_middle; never T_j_set.")
        converted = {key: float(values[key]) for key in PROCESS_FIELDS}
        if any(not math.isfinite(value) for value in converted.values()):
            raise ValueError("Process values must be finite.")
        if converted["T"] <= -273.15 or converted["T_j"] <= -273.15 or any(
            converted[key] < 0 for key in ("c", "count_middle")
        ):
            raise ValueError("Process values are outside physical bounds.")
        # One UA Write request; failure is fatal for this run, not silently retried.
        self._client.write_values(
            [self._nodes[key] for key in PROCESS_FIELDS],
            [ua.Variant(converted[key], ua.VariantType.Double) for key in PROCESS_FIELDS],
        )

    def commissioning_write(self, values: Mapping[str, float], *, inactive_checked: bool = False) -> None:
        """Explicit commissioning only; PlantService never calls this method.

        Caller must check Controller and generator inactivity immediately before
        each call. This narrowly scoped escape hatch initializes the test fixture
        and checks the setpoint node, not the production command/write protocol.
        """
        if not inactive_checked or not self.connected:
            raise ProsysVerificationError("Commissioning needs explicit inactive checks and verified Prosys.")
        if not values or set(values) - set(DEFAULT_IDENTIFIERS):
            raise ValueError("Commissioning can write only the five configured test nodes.")
        converted = {key: float(value) for key, value in values.items()}
        if any(not math.isfinite(value) for value in converted.values()):
            raise ValueError("Commissioning values must be finite.")
        for key, value in converted.items():
            if key in {"T", "T_j", "T_j_set"} and not -20 <= value <= 180:
                raise ValueError("Commissioning temperatures must be within the approved test limits.")
            if key in {"c", "count_middle"} and value < 0:
                raise ValueError("Commissioning concentration/count cannot be negative.")
        self._client.write_values([self._nodes[key] for key in converted],
                                  [ua.Variant(value, ua.VariantType.Double) for value in converted.values()])


__all__ = ["ProsysConfig", "ProsysProcessClient", "ProsysVerificationError"]
