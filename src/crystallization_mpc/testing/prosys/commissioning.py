"""Explicit commissioning of real Prosys plus the existing Telegraf read path.

No control algorithm runs here. Probe is read-only; write modes require both
an explicit flag and currently inactive Controller/test-generator HTTP status.
Reports distinguish node/read-path checks from untested full closed-loop gates.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from urllib.request import urlopen
from uuid import uuid4

from crystallization_mpc.apps.controller.algorithm.parameters import build_parameters
from crystallization_mpc.apps.controller.rabbit_process import RabbitMqProcessAdapter, RabbitProcessConfig
from .opcua_client import ProsysConfig, ProsysProcessClient
from .plant import SOURCE, _atomic_json, utc_now
from .raw_store import RawStoreVerifier, load_test_influx_settings, timestamp_ns


STATIC_VALUES = {"T": 25., "T_j": 23., "c": .32, "count_middle": 17., "T_j_set": 22.}
UNTESTED = {
    "controller_command_writer_receipt": "NOT RUN: no Controller cycle or writer command is issued by commissioning",
    "dynamic_response": "NOT RUN: static commissioning does not exercise process dynamics",
    "four_phase_paper_protocol": "NOT RUN",
    "hardware_and_real_camera": "NOT RUN",
}


def _get_json(url: str) -> Mapping[str, Any]:
    with urlopen(url, timeout=3) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("Status endpoint did not return an object.")
    return value


@dataclass
class InactiveGuard:
    controller_url: str
    plant_url: str
    execute_writes: bool = False
    get_json: Callable[[str], Mapping[str, Any]] = _get_json

    def check(self) -> dict[str, Any]:
        if not self.execute_writes:
            raise ValueError("This action requires --execute-writes.")
        if not self.controller_url or not self.plant_url:
            raise ValueError("Explicit Controller and plant status URLs are required before writes.")
        controller = self.get_json(self.controller_url.rstrip("/") + "/api/status")
        plant = self.get_json(self.plant_url.rstrip("/") + "/status")
        if controller.get("active") is not False or controller.get("status") not in {"idle", "configured", "stopped", "error"}:
            raise ValueError("Controller is active or its inactive state cannot be proven.")
        if plant.get("source") != SOURCE or plant.get("running") is not False or plant.get("status") not in {"unprepared", "stopped", "error"}:
            raise ValueError("Test plant is publishing/running or its inactive identity cannot be proven.")
        return {"checked_at": utc_now(), "controller_status": controller["status"], "plant_status": plant["status"]}


def values_match(actual: Mapping[str, Any], expected_C: Mapping[str, float], *, controller_units: bool) -> bool:
    for key, expected in expected_C.items():
        if controller_units and key in {"T", "T_j", "T_j_set"}:
            expected += 273.15
        if key not in actual or not math.isfinite(float(actual[key])) or not math.isclose(float(actual[key]), expected, rel_tol=0, abs_tol=1e-8):
            return False
    return True


class TelegrafRoundReader:
    """Dedicated subscriber using the production five-node assembly/validation.

    It never calls write_jacket_setpoint. Separate uniquely named queues avoid
    consuming messages intended for Controller. Only these own queues are
    removed on close; no existing queue or experiment is purged.
    """
    def __init__(self, config: ProsysConfig, *, rabbit_url: str, exchange: str = "idp.bus",
                 device_id: str = "crystallizer", measurement_name: str = "crystallizer_raw",
                 startup_timeout_s: float = 10.0):
        suffix = uuid4().hex
        self.config = RabbitProcessConfig(
            rabbit_url=rabbit_url, exchange=exchange, device_id=device_id,
            node_ids=config.node_ids(), measurement_name=measurement_name,
            startup_timeout_s=startup_timeout_s,
            process_queue=f"prosys.commission.{suffix}.telemetry",
            result_queue=f"prosys.commission.{suffix}.results",
        )
        self.adapter = RabbitMqProcessAdapter(self.config)

    def connect(self) -> None:
        self.adapter.connect()

    def read(self) -> dict[str, Any]:
        state = self.adapter.read_state().to_dict()
        status = self.adapter.status()
        return {**state, "invalid_message_count": status["invalid_message_count"],
                "reconnect_count": status["reconnect_count"],
                "collection_timestamp_ns": timestamp_ns(state["read_at"])}

    def close(self) -> None:
        self.adapter.disconnect()
        # This client knows the exact queue identities it alone created.
        connection = RabbitMqProcessAdapter._default_connection(self.config.rabbit_url)
        try:
            channel = connection.channel()
            for queue in (self.config.process_queue, self.config.result_queue):
                channel.queue_delete(queue=queue, if_unused=True, if_empty=False)
        finally:
            connection.close()


class Commissioning:
    def __init__(self, client: ProsysProcessClient, guard: InactiveGuard | None = None, *,
                 reader: Any = None, raw_store: RawStoreVerifier | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.client, self.guard, self.reader = client, guard, reader
        self.raw_store = raw_store
        self.clock, self.sleep = monotonic, sleep
        self.evidence: dict[str, Any] = {"source": "Real Prosys commissioning", "started_at": utc_now(),
                                       "checks": {}, "not_run": dict(UNTESTED), "writes": []}

    def _write(self, values: Mapping[str, float]) -> None:
        if self.guard is None:
            raise ValueError("A verified inactive guard is required for writes.")
        safety = self.guard.check()
        self.client.commissioning_write(values, inactive_checked=True)
        self.evidence["writes"].append({"at": utc_now(), "values_C": dict(values), "inactive_guard": safety})

    def probe(self) -> None:
        try:
            self.evidence["server_identity"] = self.client.connect()
        except Exception:
            identity = self.client.server_identity
            if identity is not None:
                self.evidence["server_identity"] = dict(identity)
                self.evidence["checks"]["prosys_product_identity"] = "PASS" if identity.get("product_identity_verified") else "FAIL"
            else:
                self.evidence["checks"]["prosys_product_identity"] = "FAIL"
            self.evidence["checks"]["prosys_node_contract"] = "FAIL"
            raise
        self.evidence["node_values"] = self.client.read_values()
        self.evidence["checks"]["prosys_node_contract"] = "PASS"

    def initialize_static(self) -> None:
        self.probe()
        self._write(STATIC_VALUES)
        values = self.client.read_values()
        if not values_match(values, STATIC_VALUES, controller_units=False):
            raise RuntimeError("Static fixture write/readback mismatch.")
        self.evidence["checks"]["static_initialization"] = "PASS"
        self.evidence["node_values"] = values

    def collect(self, expected: Mapping[str, float], *, rounds: int, timeout_s: float,
                after_timestamp: str | None = None) -> list[dict[str, Any]]:
        if self.reader is None:
            raise ValueError("Real Telegraf/RabbitMQ subscriber is required.")
        deadline = self.clock() + timeout_s
        results: list[dict[str, Any]] = []
        stamps: set[str] = set()
        previous: dict[str, Any] | None = None
        while self.clock() < deadline:
            current = self.reader.read()
            stamp = current["read_at"]
            if stamp not in stamps and stamp != after_timestamp and values_match(current, expected, controller_units=True):
                if previous is not None:
                    from datetime import datetime
                    elapsed = (datetime.fromisoformat(stamp.replace("Z", "+00:00")) - datetime.fromisoformat(previous["read_at"].replace("Z", "+00:00"))).total_seconds()
                    if not 0 < elapsed <= 2.5 or current["invalid_message_count"] != previous["invalid_message_count"] or current["reconnect_count"] != previous["reconnect_count"]:
                        raise RuntimeError("Telegraf rounds were not consecutive healthy one-second collections.")
                results.append(current)
                stamps.add(stamp)
                previous = current
                if len(results) >= rounds:
                    return results
            elif stamp not in stamps and previous is not None and stamp != after_timestamp:
                raise RuntimeError("A successive Telegraf round differed from the static fixture.")
            self.sleep(.05)
        raise TimeoutError(f"Only {len(results)}/{rounds} healthy distinct Telegraf rounds received.")

    def verify_static(self, *, rounds: int = 60) -> None:
        if self.raw_store is None:
            raise ValueError("Formal static verification also requires independent raw InfluxDB evidence.")
        self.probe()
        self.guard.check()
        self.reader.connect()
        values = self.client.read_values()
        if not values_match(values, STATIC_VALUES, controller_units=False):
            raise RuntimeError("Initialize the five static fixture values first.")
        collected = self.collect(STATIC_VALUES, rounds=rounds, timeout_s=max(20, rounds * 2.5))
        self.evidence["static_telegraf_rounds"] = collected
        self.evidence["checks"]["telegraf_five_values_and_kelvin_conversion"] = "PASS"
        self._write({"T": 26.0})
        try:
            changed = self.collect({**STATIC_VALUES, "T": 26.0}, rounds=1, timeout_s=10,
                                   after_timestamp=collected[-1]["read_at"])
            self.evidence["changed_temperature_round"] = changed[0]
            self.evidence["checks"]["telegraf_change_25C_to_299_15K"] = "PASS"
        finally:
            self._write({"T": 25.0})
            restored = self.client.read_values()
            self.evidence["temperature_restored"] = values_match(restored, {"T": 25.0}, controller_units=False)
            if not self.evidence["temperature_restored"]:
                raise RuntimeError("Failed to restore the static temperature fixture.")
        try:
            self.evidence["raw_influx_verification"] = self.raw_store.verify([*collected, changed[0]])
            self.evidence["checks"]["telegraf_raw_influx_persistence"] = "PASS"
        except Exception:
            self.evidence["checks"]["telegraf_raw_influx_persistence"] = "FAIL"
            self.evidence["raw_influx_verification"] = dict(self.raw_store.last_evidence)
            raise

    def writecheck(self) -> None:
        self.probe()
        self._write({"T_j_set": 37.0})
        try:
            immediate = self.client.read_values()["T_j_set"]
            self.client.close()
            self.client.connect()  # product/contract checked again after reconnect
            reconnected = self.client.read_values()["T_j_set"]
            self.evidence["setpoint_readback"] = {"immediate_C": immediate, "after_reconnect_C": reconnected}
            if not math.isclose(immediate, 37., abs_tol=1e-8) or not math.isclose(reconnected, 37., abs_tol=1e-8):
                raise RuntimeError("Setpoint write did not persist independently of the client connection.")
            self.evidence["checks"]["setpoint_node_write_readback_reconnect"] = "PASS"
        finally:
            if not self.client.connected:
                self.client.connect()
            self._write({"T_j_set": 22.0})
            self.evidence["setpoint_restored"] = values_match(self.client.read_values(), {"T_j_set": 22.}, controller_units=False)
            if not self.evidence["setpoint_restored"]:
                raise RuntimeError("Failed to restore the static setpoint fixture.")

    def prepare_dynamic(self) -> None:
        self.probe()
        initial = float(build_parameters({"target": "sigma"})["T_j_set_init"]) - 273.15
        self._write({"T_j_set": initial})
        if not values_match(self.client.read_values(), {"T_j_set": initial}, controller_units=False):
            raise RuntimeError("Dynamic initial setpoint readback mismatch.")
        self.evidence["checks"]["dynamic_initial_setpoint"] = "PASS"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["probe", "initialize-static", "verify-static", "writecheck", "prepare-dynamic"], nargs="?", default="probe")
    parser.add_argument("--report", type=Path, required=True, help="New evidence file; existing reports are never overwritten")
    parser.add_argument("--execute-writes", action="store_true")
    parser.add_argument("--controller-url", default=os.getenv("PROSYS_TEST_CONTROLLER_URL", ""))
    parser.add_argument("--plant-url", default=os.getenv("PROSYS_TEST_PLANT_URL", ""))
    parser.add_argument("--telemetry-config", type=Path,
                        default=Path(os.getenv("PROSYS_TEST_TELEMETRY_CONFIG", ".runtime/prosys-test/telemetry.json")),
                        help="Explicit isolated Influx credentials file; required for verify-static, token is never reported")
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("Report already exists; choose a new evidence path.")
    config = ProsysConfig.from_env()
    client = ProsysProcessClient(config)
    reader = None
    raw_store = None
    if args.action == "verify-static":
        rabbit_url = os.environ.get("RABBIT_URL")
        if not rabbit_url:
            parser.error("RABBIT_URL is required to inspect real Telegraf messages.")
        reader = TelegrafRoundReader(config, rabbit_url=rabbit_url,
                                    exchange=os.getenv("RABBIT_EXCHANGE", "idp.bus"),
                                    device_id=os.getenv("PROCESS_DEVICE_ID", "crystallizer"),
                                    measurement_name=os.getenv("PROCESS_MEASUREMENT_NAME", "crystallizer_raw"))
        raw_store = RawStoreVerifier(
            load_test_influx_settings(args.telemetry_config), config.node_ids(),
            device_id=os.getenv("PROCESS_DEVICE_ID", "crystallizer"),
            measurement_name=os.getenv("PROCESS_MEASUREMENT_NAME", "crystallizer_raw"),
        )
    guard = InactiveGuard(args.controller_url, args.plant_url, args.execute_writes)
    checks = Commissioning(client, guard, reader=reader, raw_store=raw_store)
    result = 0
    try:
        getattr(checks, args.action.replace("-", "_"))()
        checks.evidence["action_status"] = "PASS"
    except Exception as exc:
        result = 1
        checks.evidence["action_status"] = "FAIL"
        checks.evidence["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for name, resource in (("telegraf_reader", reader), ("opcua_client", client)):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    result = 1
                    checks.evidence["action_status"] = "FAIL"
                    checks.evidence.setdefault("cleanup_errors", {})[name] = str(exc)
        checks.evidence.update({"action": args.action, "ended_at": utc_now(), "overall_integration": "NOT RUN"})
        _atomic_json(args.report, checks.evidence)
    print(json.dumps({"action": args.action, "exit_code": result, "report": str(args.report)}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
