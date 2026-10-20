"""Host-side static fault collector for the real, separately loaded Prosys fixture.

Never changes/restarts Prosys, edits a project, starts the process generator or
relaxes its watchdog. The read-only test uses constant Prosys process nodes and
normal Central commands; it is not a dynamic/seeded experiment. The missing-node
test is a static refusal check, not a dynamic node-deletion test.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from asyncua import ua
from asyncua.sync import Client

from crystallization_mpc.testing.prosys.duplicate_check import WriterBus, load_server_evidence
from crystallization_mpc.testing.prosys.opcua_client import ProsysProcessClient
from crystallization_mpc.testing.prosys.restart_check import save_exclusive
from crystallization_mpc.testing.prosys.runner import (
    EvidenceLog, HttpReadTimeout, JsonHttpClient, ProsysRunner, RunnerConfig, RunnerError,
    epoch, same_start, source_manifest, utc_now,
)


class StaticSafetyFailure(RunnerError):
    """A captured unsafe/false-success behavior, not missing evidence."""


def node_snapshot(node) -> dict[str, Any]:
    value = node.read_data_value()
    number = value.Value.Value
    if (not value.StatusCode.is_good() or value.Value.VariantType != ua.VariantType.Double
            or isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)
            or node.read_node_class() != ua.NodeClass.Variable):
        raise RunnerError("Fixture node is not a finite Good scalar Double Variable.")
    return {"value": number, "quality": str(value.StatusCode), "type": value.Value.VariantType.name,
        "source_timestamp": value.SourceTimestamp.isoformat() if value.SourceTimestamp else None,
        "server_timestamp": value.ServerTimestamp.isoformat() if value.ServerTimestamp else None,
        "user_access": sorted(item.name for item in node.get_user_access_level())}


def verify_server(client, reference: dict[str, Any]) -> dict[str, Any]:
    build = client.get_node(ua.ObjectIds.Server_ServerStatus_BuildInfo).read_value()
    namespaces = list(client.get_node(ua.ObjectIds.Server_NamespaceArray).read_value())
    actual = {"product_uri": str(build.ProductUri), "software_version": str(build.SoftwareVersion),
        "build_number": str(build.BuildNumber),
        "server_uris": list(client.get_node(ua.ObjectIds.Server_ServerArray).read_value()),
        "namespace_uri": namespaces[reference["namespace_index"]],
        "product_name": str(build.ProductName), "manufacturer": str(build.ManufacturerName),
        "server_start_time": client.get_node(ua.ObjectIds.Server_ServerStatus_StartTime).read_value().isoformat()}
    if ("prosys" not in (actual["product_name"] + actual["manufacturer"]).casefold()
            or "simulation" not in actual["product_name"].casefold()
            or any(actual[key] != reference[key] for key in
                   ("product_uri", "software_version", "build_number", "server_uris", "namespace_uri"))):
        raise RunnerError("Actual native Prosys identity/namespace differs from verified reference.")
    return actual


def guard_inactive(http) -> dict[str, Any]:
    controller = http.request("GET", "http://127.0.0.1:18002/api/status")
    central = http.request("GET", "http://127.0.0.1:18000/api/system/status")
    plant = http.request("GET", "http://127.0.0.1:18010/status")
    gsensor = http.request("GET", "http://127.0.0.1:18001/status")
    current = central.get("current_experiment")
    if (controller.get("active") is not False or controller.get("status") not in {"idle", "configured", "stopped", "error"}
            or controller.get("status") == "recovery_required"
            or controller.get("recovery", {}).get("requires_confirmation")
            or (controller.get("current_run_id") and controller.get("started_at") and not controller.get("stopped_at"))
            or controller.get("integrations", {}).get("process", {}).get("write_in_flight") is not False
            or (current is not None and current.get("status") not in {"completed", "error"})
            or plant.get("running") is not False or plant.get("status") not in {"unprepared", "stopped", "error"}
            or gsensor.get("synthetic") is not True or gsensor.get("active") is not False):
        raise RunnerError("Active, held, unended or unknown services prevent this static fault test.")
    return {"controller": controller, "central": central, "plant": plant, "gsensor": gsensor}


def check_readonly_wire(command, receipt, run_id, started_at) -> None:
    from crystallization_mpc.apps.opcua_writer.commands import WriteCommand
    WriteCommand.from_envelope(command)
    payload, result = command["payload"], receipt["payload"]
    if (receipt.get("name") != "process.setpoint.result" or receipt.get("src") != "opcua_writer"
            or receipt.get("dst") != "process" or receipt.get("msg_type") != "event"
            or payload.get("run_id") != run_id or not same_start(payload.get("run_started_at"), started_at)
            or result.get("run_id") != run_id or not same_start(result.get("run_started_at"), started_at)
            or result.get("command_id") != payload.get("command_id") or result.get("tick_seq") != payload.get("tick_seq")
            or receipt.get("correlation_id") != payload.get("command_id")
            or result.get("status") != "failed" or "BadNotWritable" not in str(result.get("error"))
            or result.get("write_result") is not None):
        raise StaticSafetyFailure("Actual Writer result does not prove an identity-matched BadNotWritable failure.")


def check_failed_controller_row(status, command) -> None:
    payload = command["payload"]
    row = status.get("last_control_output") or {}
    process = status.get("integrations", {}).get("process", {})
    if (status.get("current_run_id") != payload["run_id"]
            or not same_start(status.get("started_at"), payload["run_started_at"])
            or row.get("run_id") != payload["run_id"] or row.get("tick_seq") != payload["tick_seq"]
            or row.get("result", {}).get("valid") is not False
            or row.get("process_write_attempted") is not True or row.get("process_write") is not None
            or "BadNotWritable" not in str(row.get("process_write_error"))
            or row.get("diagnostics") is not None or row.get("diagnostics_capture_error") != "invalid_output"
            or (process.get("last_command") or {}).get("command_id") != payload["command_id"]):
        raise StaticSafetyFailure("Controller's failed attempt does not match the captured rejected Writer command.")


def check_archived_failure(directory: Path, command) -> None:
    payload = command["payload"]
    with (directory / "events.csv").open(encoding="utf-8-sig", newline="") as stream:
        events = [row for row in csv.DictReader(stream) if row.get("command_id") == payload["command_id"]]
    for service, stage in (("controller", "receipt"), ("opcua_writer", "result")):
        rows = [row for row in events if row.get("service") == service and row.get("stage") == stage]
        if (len(rows) != 1 or rows[0].get("status") != "failed" or rows[0].get("run_id") != payload["run_id"]
                or int(rows[0].get("tick_seq", -1)) != payload["tick_seq"]
                or "BadNotWritable" not in str(rows[0].get("error"))):
            raise RunnerError("Archive lacks the exact failed Writer result and Controller receipt.")
    with (directory / "controller_data.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if (len(rows) != 1 or int(rows[0]["tick_seq"]) != payload["tick_seq"]
            or rows[0].get("valid", "").lower() != "false"
            or rows[0].get("process_write_attempted", "").lower() != "true"
            or rows[0].get("diag_capture_error") != "invalid_output"
            or "BadNotWritable" not in rows[0].get("error", "")):
        raise RunnerError("Archived invalid Controller attempt differs from the observed rejected write.")


def collect(output: Path, reference_path: Path, *, mode: str, execute: bool) -> dict[str, Any]:
    if execute is not True or mode not in {"readonly", "missing-count"}:
        raise ValueError("An explicit supported static fault mode and execute flag are required.")
    config, reference, reference_file = load_server_evidence(reference_path)
    log = EvidenceLog(output, utc_now)
    http, client, bus, runner = JsonHttpClient(timeout_s=5), None, None, None
    captures, wires, errors, checks = [], [], [], {}
    origin = time.monotonic()
    operation_deadline, final_deadline = origin + 540, origin + 600
    started_sources = source_manifest()
    before = after = matched_command = None
    harness_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def capture(kind, body):
        item = {"kind": kind, "observed_at": utc_now(), "body": deepcopy(body)}
        captures.append(item)
        log.append(kind, item)
        return item

    def unchanged_plant(expected):
        state = http.request("GET", "http://127.0.0.1:18010/status")
        if (state.get("running") is not False or any(state.get(key) != expected.get(key) for key in
                ("run_id", "experiment_started_at", "status"))):
            raise RunnerError("An independent process generator became active/changed; refusing static test continuation.")

    def drain():
        if bus is None:
            return
        bus.connection.process_data_events(time_limit=.01)
        for _ in range(256):
            if time.monotonic() >= operation_deadline:
                raise TimeoutError("Static fault evidence collection deadline expired; no mutation retry.")
            method, _properties, wire = bus.channel.basic_get(bus.queue, auto_ack=False)
            if method is None:
                break
            bus.channel.basic_ack(method.delivery_tag)
            message = json.loads(wire)
            item = {"observed_at": utc_now(), "raw_utf8": wire.decode("utf-8"),
                "sha256": hashlib.sha256(wire).hexdigest(), "message": message}
            wires.append(item)
            log.append("raw_writer_wire", item)
        else:
            raise RunnerError("More than 256 pending Writer envelopes; stop bounded collection rather than monopolizing the fixture.")

    try:
        initial = capture("inactive_guard", guard_inactive(http))["body"]
        client = Client(config.endpoint, timeout=3, sync_wrapper_timeout=3)
        client.connect()
        capture("native_server", verify_server(client, reference))
        nodes = {key: client.get_node(value) for key, value in reference["node_ids"].items()}
        if mode == "missing-count":
            for key in nodes.keys() - {"count_middle"}:
                capture("present_node_" + key, node_snapshot(nodes[key]))
            try:
                node_snapshot(nodes["count_middle"])
            except Exception as exc:
                capture("missing_count_read", {"error_type": type(exc).__name__, "error": str(exc), "code": getattr(exc, "code", None)})
                if getattr(exc, "code", None) != ua.StatusCodes.BadNodeIdUnknown:
                    raise
            else:
                raise RunnerError("Expected missing count node exists; wrong fixture loaded.")
            probe = ProsysProcessClient(config)
            try:
                try:
                    probe.connect()
                except Exception as exc:
                    capture("five_node_client_refusal", {"connected": probe.connected, "error_type": type(exc).__name__, "error": str(exc)})
                    if probe.connected or getattr(exc, "code", None) != ua.StatusCodes.BadNodeIdUnknown:
                        raise RunnerError("Five-node client was not rejected by the actual missing node.") from exc
                else:
                    raise RunnerError("Five-node client incorrectly accepted a missing count node.")
            finally:
                probe.close()
            capture("inactive_after", guard_inactive(http))
            checks["static_missing_node_refused"] = "PASS"
        else:
            before = {key: node_snapshot(node) for key, node in nodes.items()}
            capture("nodes_before", before)
            if ("CurrentWrite" in before["T_j_set"]["user_access"] or any(
                    "CurrentWrite" not in value["user_access"] for key, value in before.items() if key != "T_j_set")):
                raise RunnerError("Exactly the setpoint must be read-only; wrong Prosys project is loaded.")
            bus = WriterBus()
            bus.subscribe()
            from crystallization_mpc.apps.opcua_writer.commands import COMMAND_ROUTE
            bus.channel.queue_bind(queue=bus.queue, exchange="idp.bus", routing_key=COMMAND_ROUTE)
            runner = ProsysRunner(RunnerConfig(output=output / "static-run", mode="fault-fixture",
                fault_fixture_scope="static-readonly", commissioning_evidence=reference_path), client=http)
            runner.operation_deadline = operation_deadline
            runner.preflight()
            runner.server_identity = {**reference, "node_contract_verified": False,
                "contract_exception": "Intentional read-only setpoint static fault fixture"}
            previous = runner.call("central", "GET", "/api/experiments")
            created = runner.call("central", "POST", "/api/experiments", {
                "label": "Prosys static read-only setpoint fault / no process generator", "expected_run_id": previous.get("current_run_id")})
            runner.run_id = created["run_id"]
            params = runner.call("central", "GET", "/api/params")
            defaults = params["defaults"]
            params = runner.call("central", "POST", "/api/params", {"version": params["version"],
                "shared": {**defaults["shared"], "dt": 15, "dt_G": 15}, "gsensor": defaults["gsensor"],
                "controller": defaults["controller"], "expected_run_id": runner.run_id})
            runner.call("central", "PUT", "/api/run-configuration", {"run_type": "experiment", "controller_mode": "MPC",
                "control_target": "sigma", "adaptation_enabled": False, "adaptation_mode": "E_A",
                "growth_rate_source": "live_gsensor", "expected_run_id": runner.run_id})
            runner._probe_fresh_telegraf(utc_now())
            draft = runner.call("central", "GET", "/api/params")
            response = runner.call("central", "POST", "/api/operation/experiment/start", {
                **{key: draft[key] for key in ("version", "shared", "gsensor", "controller")}, "expected_run_id": runner.run_id})
            experiment = response.get("experiment") or {}
            if experiment.get("run_id") != runner.run_id or not experiment.get("started_at"):
                raise RunnerError("Start omitted the authoritative identity; do not retry the mutation.")
            runner.started_at = experiment["started_at"]
            runner.origin = time.monotonic() - max(0., epoch(utc_now()) - epoch(runner.started_at))
            deadline, baseline, fault = time.monotonic() + 60, None, None
            while time.monotonic() < deadline:
                unchanged_plant(initial["plant"])
                try:
                    status = runner.call("controller", "GET", "/api/status")
                except HttpReadTimeout:
                    unchanged_plant(initial["plant"])
                    log.append("readonly_status_timeout", {"deadline_remaining_s": max(0., deadline - time.monotonic()), "mutation_retried": False})
                    continue
                capture("controller_observation", status)
                drain()
                if (status.get("current_run_id") != runner.run_id or not same_start(status.get("started_at"), runner.started_at)):
                    if baseline is None and status.get("active") is False:
                        time.sleep(.25)
                        continue
                    raise RunnerError("Controller identity changed during static fault observation.")
                if status.get("status") == "running" and status.get("active") is True:
                    process = status.get("integrations", {}).get("process", {})
                    if process.get("sample_valid") is True and process.get("missing_fields") == []:
                        baseline = baseline or capture("healthy_static_baseline", status)
                elif status.get("status") == "error" and status.get("active") is False:
                    fault = capture("terminal_static_failure", status)
                    break
                time.sleep(.25)
            if baseline is None or fault is None:
                raise RunnerError("Healthy fresh static startup and terminal write failure were not both observed.")
            deadline = time.monotonic() + 31
            while time.monotonic() < deadline:
                unchanged_plant(initial["plant"])
                status = runner.call("controller", "GET", "/api/status")
                capture("noresume_observation", status)
                drain()
                if (status.get("current_run_id") != runner.run_id or not same_start(status.get("started_at"), runner.started_at)
                        or status.get("active") is not False or status.get("status") != "error"
                        or status.get("last_control_output") != fault["body"].get("last_control_output")
                        or status.get("scheduler", {}).get("tick_count") != fault["body"].get("scheduler", {}).get("tick_count")):
                    raise StaticSafetyFailure("Failed static run resumed/advanced without End.")
                time.sleep(1)
            drain()
            # Raw command/result envelopes are captured from production routes,
            # not reconstructed from Controller's status or inferred from a plot.
            command_ids = {item["message"].get("payload", {}).get("command_id") for item in wires
                if item["message"].get("payload", {}).get("setpoint_K") is not None
                and item["message"].get("payload", {}).get("run_id") == runner.run_id}
            if len(command_ids) != 1:
                raise StaticSafetyFailure("Expected exactly one new Controller write command, not zero or repeated attempts.")
            command_id = next(iter(command_ids))
            command_matches = [item["message"] for item in wires if item["message"].get("payload", {}).get("command_id") == command_id
                and item["message"].get("payload", {}).get("setpoint_K") is not None]
            results = [item["message"] for item in wires if item["message"].get("payload", {}).get("command_id") == command_id
                and item["message"].get("payload", {}).get("status") is not None]
            if len(command_matches) != 1 or len(results) != 1:
                raise RunnerError("Expected one actual command and one actual Writer result envelope.")
            check_readonly_wire(command_matches[0], results[0], runner.run_id, runner.started_at)
            check_failed_controller_row(fault["body"], command_matches[0])
            matched_command = command_matches[0]
            if abs(command_matches[0]["payload"]["setpoint_K"] - (before["T_j_set"]["value"] + 273.15)) < .05:
                raise RunnerError("Rejected candidate did not differ enough from the prior actual setpoint for readback evidence.")
            output_row = fault["body"].get("last_control_output") or {}
            if output_row.get("result", {}).get("valid") is not False:
                raise StaticSafetyFailure("Failed write was reported as a valid committed Controller cycle.")
            after = {key: node_snapshot(node) for key, node in nodes.items()}
            capture("nodes_after", after)
            if any(after[key]["value"] != before[key]["value"] for key in before):
                raise StaticSafetyFailure("Static process/setpoint values changed during the rejected write.")
            checks.update(readonly_access="PASS", writer_BadNotWritable="PASS", controller_failed_cycle="PASS", no_auto_resume="PASS", unchanged_static_values="PASS")
    except (Exception, KeyboardInterrupt) as exc:
        errors.append({"type": type(exc).__name__, "message": str(exc), "unsafe_observation": isinstance(exc, StaticSafetyFailure)})
        log.append("capture_error", errors[-1])
    finally:
        if runner is not None:
            runner.operation_deadline = final_deadline
            try:
                if runner.run_id:
                    system = runner.call("central", "GET", "/api/system/status")
                    if (system.get("current_experiment") or {}).get("run_id") != runner.run_id:
                        raise RunnerError("Refusing End: static fixture lost ownership.")
                    runner.call("central", "POST", f"/api/experiments/{runner.run_id}/finish", {"expected_run_id": runner.run_id})
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        status = runner.call("controller", "GET", "/api/status")
                        experiment = runner.call("central", "GET", f"/api/experiments/{runner.run_id}")
                        if (status.get("current_run_id") == runner.run_id and status.get("status") == "stopped"
                                and status.get("active") is False and status.get("stopped_at")
                                and experiment.get("status") in {"completed", "error"}):
                            capture("owned_end_confirmed", status)
                            break
                        time.sleep(.5)
                    else:
                        raise RunnerError("Explicit End was not confirmed; no automatic retry.")
                    if runner.started_at:
                        runner._validate_completed_archive()
                        capture("ordinary_archive_validation", runner.archive_validation)
                        if runner.archive_validation.get("status") != "PASS":
                            raise RunnerError("Static fault-run archive is incomplete/failed; it cannot certify acceptance.")
                        if matched_command is not None:
                            check_archived_failure(output / "static-run", matched_command)
                            checks["persisted_failed_command_and_attempt"] = "PASS"
                runner.write_report()
            except Exception as exc:
                errors.append({"phase": "owned_end_archive", "type": type(exc).__name__, "message": str(exc)})
            finally:
                runner.log.close()
        for name, resource in (("bus", bus), ("opcua", client)):
            if resource is not None:
                try:
                    resource.close() if name == "bus" else resource.disconnect()
                except Exception as exc:
                    errors.append({"phase": "cleanup_" + name, "type": type(exc).__name__, "message": str(exc)})
        final_sources = source_manifest()
        if time.monotonic() > final_deadline:
            errors.append({"phase": "total_budget", "message": "Static collector exceeded 600 seconds; no timing PASS."})
        if final_sources["source_sha256"] != started_sources["source_sha256"]:
            errors.append({"phase": "source_unchanged", "message": "Host source changed during capture."})
        evidence = {"scope": "Static native Prosys fault fixture; no external process generator, G, seed, dynamic response or paper acceptance.",
            "mode": mode, "reference": reference_file, "run_id": runner.run_id if runner else None,
            "started_at": runner.started_at if runner else None, "captures": captures, "wires": wires,
            "errors": errors, "checks": checks, "source_manifest": started_sources, "source_manifest_at_end": final_sources,
            "host_harness_sha256": harness_hash, "completed_at": utc_now()}
        result = {"status": "FAIL" if any(item.get("unsafe_observation") for item in errors) else "PASS" if checks and not errors else "INCOMPLETE", "mode": mode, "checks": checks,
            "errors": errors, "scope": evidence["scope"], "fault_evidence": "fault_evidence.json",
            "archive_validation": runner.archive_validation if runner else {"status": "NOT_RUN"}}
        save_exclusive(output / "fault_evidence.json", evidence)
        save_exclusive(output / "fault_result.json", result)
        log.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("readonly", "missing-count"), required=True)
    parser.add_argument("--execute-static-test", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args()
    if not args.execute_static_test:
        parser.error("--execute-static-test is required; no fault is injected by this tool")
    result = collect(args.output, args.reference, mode=args.mode, execute=True)
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
