"""Explicit standalone production-Writer duplicate test, never a Controller run.

Only --execute-write-test permits writes. Original and replay command bytes are
identical. A separate Prosys client changes and reads a sentinel between them.
Existing experiments are never created, stopped, renamed or appended to: the
Writer fixture owns a new identity and an exclusive local evidence directory.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from crystallization_mpc.apps.opcua_writer.commands import COMMAND_ROUTE, RESULT_ROUTE, WriteCommand
from crystallization_mpc.messaging.schema import build_envelope
from .fault_evidence import evaluate
from .opcua_client import ProsysConfig, ProsysProcessClient
from .restart_check import save_exclusive
from .runner import EvidenceLog, JsonHttpClient, RunnerError, epoch, finite, source_manifest, utc_now

URLS = {"central": "http://127.0.0.1:18000/api/system/status",
        "controller": "http://127.0.0.1:18002/api/status", "plant": "http://127.0.0.1:18010/status"}
IDENTITY_FIELDS = ("product_uri", "software_version", "build_number", "server_uris",
                   "namespace_uri", "namespace_index", "node_ids")


def load_server_evidence(path: Path) -> tuple[ProsysConfig, dict[str, Any], dict[str, str]]:
    raw = path.read_bytes()
    report = json.loads(raw)
    identity = report.get("server_identity") or {}
    if (report.get("action_status") != "PASS" or identity.get("product_identity_verified") is not True
            or identity.get("node_contract_verified") is not True):
        raise ValueError("A passing real Prosys node/server report is required.")
    parsed = urlparse(identity.get("endpoint", ""))
    if (parsed.scheme != "opc.tcp" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port != 53530 or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Writer fixture permits only the isolated loopback Prosys port 53530.")
    namespace = identity["namespace_index"]
    prefix = f"ns={namespace};s="
    nodes = identity["node_ids"]
    if type(namespace) is not int or namespace < 1 or not all(isinstance(value, str) and value.startswith(prefix) for value in nodes.values()):
        raise ValueError("Verified Prosys String NodeId mapping is invalid.")
    config = ProsysConfig(identity["endpoint"], namespace_index=namespace,
                          identifiers={key: value[len(prefix):] for key, value in nodes.items()})
    if not all(identity.get(key) for key in IDENTITY_FIELDS):
        raise ValueError("Prosys reference identity is incomplete.")
    return config, identity, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


class WriterBus:
    """One-attempt isolated publisher with its own exclusive result subscriber."""
    def __init__(self, *, monotonic=time.monotonic, sleep=time.sleep):
        self.monotonic, self.sleep = monotonic, sleep
        self.connection = self.channel = None
        self.queue = "prosys.duplicate." + uuid4().hex + ".results"
        self.queue_declared = False

    def subscribe(self) -> None:
        import pika
        parameters = pika.URLParameters("amqp://guest:guest@127.0.0.1:5674/%2F")
        parameters.connection_attempts = 1
        parameters.retry_delay = 0
        parameters.socket_timeout = 3
        parameters.stack_timeout = 5
        parameters.blocked_connection_timeout = 3
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange="idp.bus", passive=True)
        pending = self.channel.queue_declare(queue="opcua_writer.in", passive=True)
        if pending.method.message_count != 0:
            raise RunnerError("The isolated Writer command queue is not empty; never purge or compete with other commands.")
        self.channel.queue_declare(queue=self.queue, durable=False, exclusive=True, auto_delete=True)
        self.queue_declared = True
        self.channel.queue_bind(queue=self.queue, exchange="idp.bus", routing_key=RESULT_ROUTE)
        self.channel.confirm_delivery()

    def publish(self, wire: bytes) -> None:
        import pika
        if self.channel is None:
            raise RunnerError("Subscribe to actual Writer results before publishing.")
        message = json.loads(wire)
        confirmed = self.channel.basic_publish(exchange="idp.bus", routing_key=COMMAND_ROUTE, body=wire,
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2,
                correlation_id=message["correlation_id"]), mandatory=True)
        if confirmed is False:
            raise RunnerError("Writer command publisher confirmation is unavailable; do not retry.")

    def assert_empty(self) -> None:
        method, _properties, _wire = self.channel.basic_get(self.queue, auto_ack=False)
        if method is not None:
            self.channel.basic_ack(method.delivery_tag)
            raise RunnerError("Unexpected pending result before the planned replay; outcome is ambiguous.")

    def receive(self, command_id: str, *, guard: Callable, timeout: float = 10) -> bytes:
        deadline, next_guard = self.monotonic() + timeout, self.monotonic()
        while self.monotonic() < deadline:
            if self.monotonic() >= next_guard:
                guard()
                next_guard = self.monotonic() + .5
            method, _properties, wire = self.channel.basic_get(self.queue, auto_ack=False)
            if method is not None:
                self.channel.basic_ack(method.delivery_tag)
                message = json.loads(wire)
                if message.get("correlation_id") == command_id:
                    return wire
                raise RunnerError("An unrelated Writer result appeared during the isolated fixture.")
            self.connection.process_data_events(time_limit=.05)
            self.sleep(.05)
        raise TimeoutError("Writer result not observed; no command retry is permitted.")

    def close(self) -> None:
        try:
            if self.queue_declared and self.channel is not None and self.channel.is_open:
                self.channel.queue_delete(queue=self.queue, if_unused=False, if_empty=False)
        finally:
            if self.connection is not None and self.connection.is_open:
                self.connection.close()


def run_duplicate_check(output: Path, server_evidence: Path, *, execute_write_test: bool,
                        http: Any = None, opcua_factory: Callable = ProsysProcessClient,
                        bus_factory: Callable = WriterBus, now: Callable = utc_now,
                        monotonic: Callable = time.monotonic, sleep: Callable = time.sleep,
                        source_provider: Callable = source_manifest) -> dict[str, Any]:
    if execute_write_test is not True:
        raise ValueError("Explicit --execute-write-test is required before any write or queue mutation.")
    config, expected_server, reference = load_server_evidence(server_evidence)
    initial_sources = source_provider()
    log = EvidenceLog(output, now)  # exclusive directory; never reuse or overwrite
    http = http or JsonHttpClient(timeout_s=3)
    captures, observations = [], []
    baseline = None
    client = bus = None
    identity = None
    initial_setpoint = last_expected = None
    attempted = uncertain = False
    error = None
    cleanup = {"restore": {"status": "NOT NEEDED"}, "bus": {"status": "NOT NEEDED"}, "opcua": {"status": "NOT NEEDED"}}
    fixture_id, fixture_started = "prosys_writer_fixture_" + uuid4().hex, now()

    def capture(kind, phase, body, wire=None):
        item = {"kind": kind, "phase": phase, "observed_at": now(), "body": deepcopy(body)}
        if wire is not None:
            item["raw_utf8"] = wire.decode("utf-8")
            item["wire_sha256"] = hashlib.sha256(wire).hexdigest()
        captures.append(item)
        log.append("writer_fixture_capture", {"capture": item})
        return item

    def guard(phase=None):
        nonlocal baseline
        states = {kind: http.request("GET", url) for kind, url in URLS.items()}
        observed = {"observed_at": now(), "states": deepcopy(states)}
        observations.append(observed)
        log.append("writer_fixture_inactive_guard", observed)
        controller, central, plant = (states[key] for key in ("controller", "central", "plant"))
        current = central.get("current_experiment")
        if (controller.get("active") is not False or controller.get("status") not in {"idle", "configured", "stopped", "error"}
                or (controller.get("integrations") or {}).get("process", {}).get("write_in_flight") is not False
                or "current_experiment" not in central
                or (current is not None and (not isinstance(current, dict) or current.get("status") not in {"completed", "error"}))
                or plant.get("source") != "Synthetic G / Model-generated input" or plant.get("running") is not False
                or plant.get("status") not in {"unprepared", "stopped", "error"}):
            raise RunnerError("An active, prepared or unknown service prevents this standalone Writer fixture.")
        if baseline is None:
            baseline = deepcopy(states)
        if (any(controller.get(key) != baseline["controller"].get(key) for key in ("current_run_id", "started_at", "status"))
                or current != baseline["central"].get("current_experiment")
                or any(plant.get(key) != baseline["plant"].get(key) for key in ("run_id", "experiment_started_at", "status"))):
            raise RunnerError("Original inactive service identity changed; do not write or restore over another operation.")
        if phase:
            for kind, body in states.items():
                capture(kind, phase, body)
        return states

    def receipt(wire, command, phase):
        body = json.loads(wire)
        observed = capture("receipt", phase, body, wire)
        published = next(item for item in captures if item["kind"] == "command" and item["phase"] == phase)
        payload = body.get("payload") or {}
        write = payload.get("write_result") or {}
        if (body.get("ver") != 1 or body.get("src") != "opcua_writer" or body.get("dst") != "process"
                or body.get("name") != "process.setpoint.result" or body.get("msg_type") != "event"
                or body.get("correlation_id") != command.command_id
                or not epoch(published["observed_at"]) <= epoch(body.get("ts")) <= epoch(observed["observed_at"])
                or any(payload.get(key) != command.to_dict()[key] for key in ("command_id", "run_id", "run_started_at", "tick_seq"))
                or payload.get("status") != "success" or write.get("verified") is not True
                or not finite(write.get("readback_K")) or abs(write["readback_K"] - command.setpoint_K) > .05
                or not finite(write.get("setpoint_K")) or abs(write["setpoint_K"] - command.setpoint_K) > .05):
            raise RunnerError("Actual production Writer did not provide the matching verified terminal success.")
        return payload

    try:
        guard("before")
        client = opcua_factory(config)
        identity = client.connect()
        if (identity.get("product_identity_verified") is not True or identity.get("node_contract_verified") is not True
                or any(identity.get(key) != expected_server.get(key) for key in IDENTITY_FIELDS)):
            raise RunnerError("Live Prosys identity/node contract differs from the verified reference.")
        process_nodes = baseline["controller"].get("integrations", {}).get("process", {}).get("node_ids")
        if process_nodes != identity["node_ids"]:
            raise RunnerError("Controller's isolated node mapping differs from the actual Prosys server.")
        initial = client.read_values()
        initial_setpoint = last_expected = initial["T_j_set"]
        if not finite(initial_setpoint) or not -20 <= initial_setpoint <= 180:
            raise RunnerError("Initial setpoint cannot be safely restored within test limits.")
        capture("node_values", "before", initial)
        bus = bus_factory(monotonic=monotonic, sleep=sleep)
        bus.subscribe()
        log.append("writer_fixture_subscribed", {"queue": bus.queue, "exchange": "idp.bus", "route": RESULT_ROUTE})
        fixture_started = now()
        previous_starts = [baseline["controller"].get("started_at"), baseline["plant"].get("experiment_started_at")]
        if any(value is not None and epoch(value) >= epoch(fixture_started) for value in previous_starts):
            raise RunnerError("Writer fixture start must be later than the preserved services' experiment identities.")
        command_id = uuid4().hex
        expires = datetime.fromtimestamp(epoch(now()), timezone.utc) + timedelta(seconds=30)
        envelope = build_envelope("controller", "opcua_writer", "command", "process.setpoint.write", 1,
            {"command_id": command_id, "run_id": fixture_id, "run_started_at": fixture_started,
             "tick_seq": 1, "setpoint_K": 310.15, "expires_at": expires.isoformat()}, command_id)
        envelope["ts"] = now()
        command = WriteCommand.from_envelope(envelope)
        wire = json.dumps(envelope, separators=(",", ":"), allow_nan=False).encode()
        guard("original")
        capture("command", "original", envelope, wire)
        attempted = uncertain = True
        bus.publish(wire)
        first = receipt(bus.receive(command_id, guard=guard), command, "original")
        original_values = client.read_values()
        capture("node_values", "original", original_values)
        if abs(original_values["T_j_set"] - 37.) > .05:
            raise RunnerError("Independent original setpoint readback differs from the Writer success.")
        last_expected, uncertain = original_values["T_j_set"], False

        guard("sentinel")
        if abs(client.read_values()["T_j_set"] - last_expected) > .05:
            uncertain = True
            raise RunnerError("Setpoint changed outside the owned fixture before the sentinel.")
        uncertain = True
        log.append("writer_fixture_sentinel_intent", {"T_j_set_C": 22.})
        client.commissioning_write({"T_j_set": 22.}, inactive_checked=True)
        sentinel = client.read_values()
        capture("node_values", "sentinel", sentinel)
        if abs(sentinel["T_j_set"] - 22.) > .05:
            raise RunnerError("Independent sentinel write/readback did not verify.")
        last_expected, uncertain = sentinel["T_j_set"], False

        guard("replay")
        bus.assert_empty()
        capture("command", "replay", envelope, wire)
        uncertain = True
        bus.publish(wire)  # the one explicitly planned same-byte replay, never an error retry
        second = receipt(bus.receive(command_id, guard=guard), command, "replay")
        guard()
        replay = client.read_values()
        capture("node_values", "replay", replay)
        if not any(abs(replay["T_j_set"] - allowed) <= .05 for allowed in (22., 37.)):
            raise RunnerError("Unexpected independent setpoint ownership after replay.")
        last_expected, uncertain = replay["T_j_set"], False
        if second != first:
            raise RunnerError("Replay returned a different terminal payload; cached result contract failed.")
    except (Exception, KeyboardInterrupt) as exc:
        error = {"type": type(exc).__name__}  # exception strings may contain authenticated URLs
        log.append("writer_fixture_error", error)
    finally:
        if attempted:
            if uncertain or initial_setpoint is None:
                cleanup["restore"] = {"status": "SKIPPED UNKNOWN OUTCOME", "requires_manual_review": True}
            else:
                try:
                    guard("restore")
                    if abs(client.read_values()["T_j_set"] - last_expected) > .05:
                        raise RunnerError("Setpoint no longer has the last owned value.")
                    log.append("writer_fixture_restore_intent", {"T_j_set_C": initial_setpoint})
                    client.commissioning_write({"T_j_set": initial_setpoint}, inactive_checked=True)
                    restored = client.read_values()
                    capture("node_values", "restored", restored)
                    cleanup["restore"] = {"status": "PASS" if abs(restored["T_j_set"] - initial_setpoint) <= .05 else "FAIL",
                                          "initial_T_j_set_C": initial_setpoint, "readback_T_j_set_C": restored["T_j_set"]}
                except Exception as exc:
                    cleanup["restore"] = {"status": "INCOMPLETE", "error_type": type(exc).__name__, "requires_manual_review": True}
        for label, resource in (("bus", bus), ("opcua", client)):
            if resource is not None:
                try:
                    resource.close()
                    cleanup[label] = {"status": "PASS"}
                except Exception as exc:
                    cleanup[label] = {"status": "INCOMPLETE", "error_type": type(exc).__name__}
        try:
            final_sources = source_provider()
            document = {"mode": "writer_fixture", "run_id": fixture_id, "experiment_started_at": fixture_started,
                "source": "Standalone production Writer fixture; not a Controller experiment",
                "server_identity": identity, "server_reference": reference, "captures": captures,
                "guard_observations": observations, "cleanup": cleanup, "error": error,
                "source_manifest": initial_sources, "source_manifest_at_end": final_sources,
                "writes_attempted": attempted, "unknown_mutation_outcome": uncertain}
            result = evaluate("duplicate_command", document)
            result.update(mode="writer_fixture", cleanup=cleanup, error=error,
                scope="Production Writer deduplication only; Controller closed-loop and paper experiment NOT RUN.")
            if initial_sources.get("source_sha256") != final_sources.get("source_sha256"):
                result["status"] = "FAIL"
                result["checks"]["source_unchanged"] = {"status": "FAIL", "detail": "Source/config changed during fixture."}
            if result["status"] != "FAIL" and (error or any(value["status"] not in {"PASS", "NOT NEEDED"} for value in cleanup.values())):
                result["status"] = "INCOMPLETE"
            save_exclusive(output / "fault_evidence.json", document)
            save_exclusive(output / "fault_result.json", result)
        finally:
            log.close()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-write-test", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server-evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.execute_write_test:
        parser.error("--execute-write-test is required before any write or queue mutation")
    result = run_duplicate_check(args.output, args.server_evidence, execute_write_test=True)
    print(json.dumps({"status": result["status"], "fault_result": str(args.output / "fault_result.json")}))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
