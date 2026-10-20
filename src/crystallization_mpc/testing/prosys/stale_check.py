"""Explicit, isolated Telegraf pause test; no action without --execute-pause.

Two fresh Controller ticks precede one pause of the exact label-verified test
Telegraf container. The plant stays running. Capture the actual expired input
before Controller disconnect clears its age, prove terminal stop, restore only
our paused container, then prove recovery in a distinct owned run. Missing
observations remain INCOMPLETE; no timestamps or measurements are synthesized.
After End Experiment and dual inactive checks, the recovery fixture explicitly
re-verifies Prosys and writes/readbacks T_j_set=42 C once before that new run.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .fault_evidence import evaluate
from .restart_check import LABELS, PROJECT, configured_sample_age, run_command, save_exclusive
from .runner import EvidenceLog, ProsysRunner, RunnerConfig, RunnerError, HttpReadTimeout, epoch, finite, same_start, source_manifest, utc_now


CONTAINER = "mpcrystal-prosys_telegraf_1"
INSPECT_FORMAT = ('{"Id":{{json .ID}},"Name":{{json .Name}},"State":{'
    '"StartedAt":{{json .State.StartedAt}},"Running":{{json .State.Running}},'
    '"Paused":{{json .State.Paused}},"Status":{{json .State.Status}}},"Labels":{' +
    ",".join(json.dumps(key) + ':{{json (index .Config.Labels ' + json.dumps(key) + ')}}'
             for key in LABELS) + '}}')


def inspect_telegraf(command_runner: Callable, target: str = CONTAINER, *, observer: Callable | None = None) -> dict[str, Any]:
    raw = json.loads(command_runner(["podman", "inspect", "--type", "container", "--format", INSPECT_FORMAT, target], timeout=5))
    if not isinstance(raw, dict):
        raise RunnerError("Telegraf inspection must return one object.")
    identifier, name, labels, state = raw.get("Id"), raw.get("Name"), raw.get("Labels"), raw.get("State")
    observed = {"Id": identifier, "Name": name,
        "State": {key: (state or {}).get(key) for key in ("StartedAt", "Running", "Paused", "Status")} if isinstance(state, dict) else None,
        "Labels": {key: labels.get(key) for key in LABELS if labels.get(key)} if isinstance(labels, dict) else None}
    if observer is not None:
        observer(deepcopy(observed))  # preserve failed/uncertain inspections, never Config.Env
    if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier)
            or not isinstance(name, str) or name.lstrip("/") != CONTAINER
            or not isinstance(labels, dict) or not isinstance(state, dict)):
        raise RunnerError("The exact isolated Telegraf container could not be identified.")
    for suffix, expected in (("project", PROJECT), ("service", "telegraf")):
        present = [labels[key] for key in LABELS if key.endswith("." + suffix) and labels.get(key)]
        if not present or any(value != expected for value in present):
            raise RunnerError("Telegraf compose labels do not match the isolated test stack.")
    if type(state.get("Running")) is not bool or type(state.get("Paused")) is not bool:
        raise RunnerError("Telegraf running/paused flags must be explicit booleans.")
    epoch(state.get("StartedAt"))
    return observed


def container_running(state: Mapping[str, Any]) -> bool:
    return state.get("Status") == "running" and state.get("Running") is True and state.get("Paused") is False


def container_paused(state: Mapping[str, Any]) -> bool:
    # Podman reports false/true/paused; Docker may report true/true/paused.
    return state.get("Status") == "paused" and state.get("Paused") is True and type(state.get("Running")) is bool


def prepare_recovery_fixture(runner: Any, report_path: Path, *, client_factory: Callable | None = None) -> dict[str, Any]:
    """One guarded test initialization, not a runtime retry or Writer proof."""
    from .commissioning import Commissioning, InactiveGuard
    from .opcua_client import ProsysConfig, ProsysProcessClient
    if report_path.exists():
        raise FileExistsError(report_path)
    expected = runner.server_identity or {}
    endpoint = urlparse(expected.get("endpoint", ""))
    if (endpoint.scheme != "opc.tcp" or endpoint.port != 53530 or endpoint.username or endpoint.password
            or endpoint.hostname not in {"127.0.0.1", "localhost", "::1", "host.containers.internal"}
            or endpoint.path != "/OPCUA/SimulationServer" or endpoint.query or endpoint.fragment):
        raise RunnerError("Recovery fixture requires the verified isolated Prosys endpoint on port 53530.")
    nodes = expected.get("node_ids") or {}
    parsed = {key: re.fullmatch(r"ns=(\d+);s=(.+)", value) for key, value in nodes.items()}
    if (set(parsed) != {"T", "T_j", "c", "count_middle", "T_j_set"}
            or not all(parsed.values()) or len({item[1] for item in parsed.values()}) != 1):
        raise RunnerError("Recovery fixture needs the actual verified five String NodeIds.")
    config = ProsysConfig(endpoint="opc.tcp://127.0.0.1:53530" + endpoint.path,
        namespace_index=int(next(iter(parsed.values()))[1]), identifiers={key: item[2] for key, item in parsed.items()})
    client = (client_factory or ProsysProcessClient)(config)
    def status(url):
        body = runner.client.request("GET", url)
        is_controller = url.endswith("/api/status")
        run = body.get("current_run_id" if is_controller else "run_id")
        start = body.get("started_at" if is_controller else "experiment_started_at")
        if run != runner.run_id or not same_start(start, runner.started_at):
            raise RunnerError("Recovery fixture will not modify another experiment's process.")
        return body
    guard = InactiveGuard(runner.config.controller, runner.config.plant, True, get_json=status)
    checks = Commissioning(client, guard)
    checks.evidence["initialization_plan"] = {"T_j_set_C": 42., "maximum_write_attempts": 1, "retry_uncertain_write": False}
    failure = None
    try:
        checks.evidence["guard_before_probe"] = guard.check()
        checks.probe()
        observed = checks.evidence["server_identity"]
        identity_keys = ("product_uri", "software_version", "build_number", "server_uris", "namespace_uri", "node_ids")
        if (any(observed.get(key) != expected.get(key) for key in identity_keys)
                or not observed.get("product_identity_verified") or not observed.get("node_contract_verified")):
            raise RunnerError("Recovery fixture connected to a different or unverified Prosys server/node contract.")
        # Existing implementation rechecks both inactive services immediately
        # before its one write and never retries an uncertain write/readback.
        checks.prepare_dynamic()
        checks.evidence["readback"] = client.read_values()
        if abs(checks.evidence["readback"]["T_j_set"] - 42.) > 1e-8:
            raise RunnerError("Recovery fixture must read back exactly 42 C.")
        checks.evidence["action_status"] = "PASS"
    except (Exception, KeyboardInterrupt) as exc:
        failure = exc
        checks.evidence.update(action_status="FAIL", error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            client.close()
        except Exception as exc:
            checks.evidence.update(action_status="FAIL", cleanup_error=f"{type(exc).__name__}: {exc}")
            if failure is None:
                failure = exc
        checks.evidence.update(action="prepare-dynamic", ended_at=utc_now(), overall_integration="NOT RUN",
            preceding_run_id=runner.run_id, preceding_experiment_started_at=runner.started_at,
            expected_server_identity=expected, target_endpoint=config.endpoint)
        save_exclusive(report_path, checks.evidence)
    if failure is not None:
        raise failure
    return checks.evidence


def run_stale_check(config: RunnerConfig, *, execute_pause: bool, process_sample_max_age_s: float,
                    configuration_source: Mapping[str, Any] | None = None,
                    runner_factory: Callable = ProsysRunner, client: Any = None,
                    command_runner: Callable = run_command, monotonic: Callable = time.monotonic,
                    sleep: Callable = time.sleep, now: Callable = utc_now,
                    source_provider: Callable = source_manifest,
                    recovery_fixture: Callable = prepare_recovery_fixture) -> dict[str, Any]:
    if execute_pause is not True:
        raise ValueError("Explicit --execute-pause is required before any experiment mutation.")
    if config.mode != "short-dynamic" or not finite(process_sample_max_age_s) or not 0 < process_sample_max_age_s <= 30:
        raise ValueError("Stale capture requires short-dynamic mode and the configured age in (0, 30] seconds.")
    log = EvidenceLog(config.output, now)  # exclusive new directory, including partial failures
    captures, observations, errors, violations = [], [], [], []
    first = recovery = None
    paused_id = None
    attempted = unpause_attempted = unpaused = False
    before_container = None
    sources = source_provider()

    def capture(kind, phase, body):
        item = {"kind": kind, "phase": phase, "observed_at": now(), "body": deepcopy(body)}
        captures.append(item)
        log.append("stale_capture", item)
        return item

    def fault_error(exc, stage):
        item = {"stage": stage, "type": type(exc).__name__, "message": str(exc)}
        errors.append(item)
        log.append("stale_capture_error", item)

    def inspect(phase, target=CONTAINER):
        return inspect_telegraf(command_runner, target, observer=lambda body: log.append(
            "telegraf_inspection", {"phase": phase, "observed_at": now(), "body": body}))

    def plant_watchdog(runner):
        plant = runner.call("plant", "GET", "/status")
        log.append("plant_watchdog", {"observed_at": now(), "body": deepcopy(plant)})
        try:
            valid = (plant.get("run_id") == runner.run_id
                and same_start(plant.get("experiment_started_at"), runner.started_at)
                and plant.get("running") is True and not plant.get("error")
                and finite(plant.get("sample_age_s")) and 0 <= plant["sample_age_s"] <= 5
                and 0 <= epoch(now()) - epoch(plant["heartbeat_at"]) <= 5)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise RunnerError("Owned external plant must keep running with a fresh OPC UA heartbeat.")

    def matching(runner, status):
        if status.get("current_run_id") != runner.run_id or not same_start(status.get("started_at"), runner.started_at):
            raise RunnerError("Controller identity changed; refusing to act on an unrelated run.")

    def healthy(runner, status):
        process = (status.get("integrations") or {}).get("process") or {}
        output = status.get("last_control_output") or {}
        try:
            matching(runner, status)
            return (status.get("active") is True and status.get("status") == "running"
                and process.get("connected") is True and process.get("sample_valid") is True
                and process.get("missing_fields") == [] and finite(process.get("sample_age_s"))
                and 0 <= process["sample_age_s"] <= process_sample_max_age_s
                and 0 <= epoch(now()) - epoch(process["last_read_at"]) <= process_sample_max_age_s
                and output.get("run_id") == runner.run_id and type(output.get("tick_seq")) is int
                and output["tick_seq"] > 0 and (output.get("result") or {}).get("valid") is True
                and output.get("process_write_error") is None and (output.get("process_write") or {}).get("verified") is True
                and 0 <= epoch(now()) - epoch(output["computed_at"]) <= 35)
        except (KeyError, TypeError, ValueError, RunnerError):
            return False

    def wait_ticks(runner, count):
        deadline, ticks = monotonic() + 90, set()
        while monotonic() < deadline:
            runner.observe_once()
            plant_watchdog(runner)
            status = runner.latest_status or {}
            if healthy(runner, status):
                ticks.add(status["last_control_output"]["tick_seq"])
                if len(ticks) >= count:
                    return status
            sleep(min(config.poll_s, 1.0))
        raise RunnerError(f"No {count} distinct fresh verified Controller output(s) within 90 seconds.")

    def restore_telegraf():
        nonlocal unpause_attempted, unpaused
        if not attempted or paused_id is None:
            return
        # Do not retry an uncertain pause. Inspect the exact original ID before
        # the only permitted cleanup action; never unpause a replacement.
        inspected = inspect("before_cleanup", paused_id)
        if inspected["Id"] != paused_id or inspected["State"]["StartedAt"] != before_container["State"]["StartedAt"]:
            raise RunnerError("Telegraf identity/process changed; no replacement will be unpaused.")
        if container_paused(inspected["State"]):
            unpause_attempted = True
            log.append("unpause_intent", {"container_id": paused_id, "attempt": 1})
            try:
                command_runner(["podman", "unpause", paused_id], timeout=10)
            except Exception as exc:
                log.append("unpause_uncertain", {"error_type": type(exc).__name__, "retried": False})
            inspected = inspect("after_unpause", paused_id)
            if (inspected["Id"] != paused_id or inspected["State"]["StartedAt"] != before_container["State"]["StartedAt"]
                    or not container_running(inspected["State"])):
                raise RunnerError("The single unpause attempt was not confirmed; manual attention is required.")
        elif not container_running(inspected["State"]):
            raise RunnerError("Telegraf exited or its state is unknown; cleanup will not start it.")
        unpaused = True
        capture("telegraf", "resumed", inspected)

    def finish(runner):
        if runner is None:
            return
        try:
            runner.shutdown()
            if runner.shutdown_errors:
                raise RunnerError("Owned run cleanup failed: " + "; ".join(runner.shutdown_errors))
        finally:
            try:
                runner.write_report()
            finally:
                runner.log.close()

    expired = failed = None
    try:
        first = runner_factory(replace(config, output=config.output / "fault-run"), client=client,
                               monotonic=monotonic, sleep=sleep, now=now)
        log.append("stale_check_started", {"source_manifest": sources, "configuration_source": configuration_source})
        first.preflight()
        first.prepare()
        wait_ticks(first, 2)
        before_container = inspect("before_pause")
        capture("telegraf", "before", before_container)
        if not container_running(before_container["State"]):
            raise RunnerError("Telegraf must be running and not already paused before this test.")
        before = first.call("controller", "GET", "/api/status")
        if not healthy(first, before):
            raise RunnerError("Controller lost healthy input before pause.")
        capture("controller", "before", before)
        dt = (before.get("scheduler") or {}).get("controller_dt_s")
        if not finite(dt) or not 0 < dt <= 30:
            raise RunnerError("The actual control period must be within the bounded test window.")
        paused_id, attempted = before_container["Id"], True
        log.append("pause_intent", {"container_id": paused_id, "attempt": 1})
        try:
            command_runner(["podman", "pause", paused_id], timeout=10)
        except Exception as exc:
            log.append("pause_uncertain", {"error_type": type(exc).__name__, "retried": False})
        inspected = inspect("after_pause", paused_id)
        if (inspected["Id"] != paused_id or inspected["State"]["StartedAt"] != before_container["State"]["StartedAt"]
                or not container_paused(inspected["State"])):
            raise RunnerError("The single pause attempt was not confirmed on the original Telegraf container.")
        capture("telegraf", "paused", inspected)
        deadline = monotonic() + process_sample_max_age_s + dt + 15
        while monotonic() < deadline:
            plant_watchdog(first)
            try:
                status = first.call("controller", "GET", "/api/status")
            except HttpReadTimeout as exc:
                # A process-read timeout holds the Controller lock. Its HTTP
                # status may be unavailable before the actual terminal state
                # can be observed. Retry only this read within the same fault
                # deadline; never infer ERROR from an observation timeout.
                log.append("stale_controller_status_timeout", {"observed_at": now(), "type": type(exc).__name__})
                plant_watchdog(first)
                sleep(.2)
                continue
            matching(first, status)
            item = {"observed_at": now(), "body": deepcopy(status)}
            observations.append(item)
            log.append("stale_interim_status", item)
            process = (status.get("integrations") or {}).get("process") or {}
            if (expired is None and status.get("active") is True and status.get("status") == "running"
                    and process.get("connected") is True and process.get("sample_valid") is False
                    and finite(process.get("sample_age_s")) and process["sample_age_s"] > process_sample_max_age_s
                    and process.get("last_read_at") and epoch(now()) - epoch(process["last_read_at"]) > process_sample_max_age_s):
                expired = capture("controller", "expired_input", status)
            if expired:
                old = expired["body"].get("last_control_output") or {}
                output = status.get("last_control_output") or {}
                if (output != old and (output.get("result") or {}).get("valid") is True):
                    violations.append({"reason": "new_success_after_expiry", **item})
            if status.get("status") == "error" and status.get("active") is False:
                failed = capture("controller", "fault", status)
                break
            sleep(.2)
        else:
            if expired:
                violations.append({"reason": "no_terminal_stop_after_expiry", "observed_at": now()})
            raise RunnerError("Controller did not reach terminal process error within the bounded fault window.")
    except (Exception, KeyboardInterrupt) as exc:
        fault_error(exc, "fault_capture")
    finally:
        try:
            restore_telegraf()
        except (Exception, KeyboardInterrupt) as exc:
            fault_error(exc, "unpause_cleanup")

    try:
        if first and failed and unpaused and not errors:
            plant_watchdog(first)
            resumed = next(item for item in captures if item["kind"] == "telegraf" and item["phase"] == "resumed")
            first._probe_fresh_telegraf(resumed["observed_at"])
            plant_watchdog(first)
            capture("telegraf", "recovered_input", first.startup_telegraf_readiness)
            deadline = monotonic() + 2 * dt + 1
            while True:
                plant_watchdog(first)
                status = first.call("controller", "GET", "/api/status")
                matching(first, status)
                item = {"observed_at": now(), "body": deepcopy(status)}
                observations.append(item)
                log.append("stale_recovery_observation", item)
                if (status.get("active") is not False or status.get("status") != "error"
                        or status.get("last_control_output") != failed["body"].get("last_control_output")):
                    violations.append({"reason": "old_run_resumed_after_input_recovery", **item})
                if monotonic() >= deadline:
                    capture("controller", "after", status)
                    break
                sleep(min(1., config.poll_s))
            finish(first)
            fixture = recovery_fixture(first, config.output / "recovery-fixture.json")
            log.append("recovery_fixture", {"action_status": fixture.get("action_status"), "report": "recovery-fixture.json"})
            if fixture.get("action_status") != "PASS":
                raise RunnerError("The explicit recovery fixture did not pass.")
            recovery = runner_factory(replace(config, output=config.output / "recovery-run"), client=client,
                                      monotonic=monotonic, sleep=sleep, now=now)
            recovery.preflight()
            recovery.prepare()
            if recovery.run_id == first.run_id or epoch(recovery.started_at) <= epoch(first.started_at):
                raise RunnerError("Recovery must create a distinct later owned experiment.")
            capture("controller", "recovered", wait_ticks(recovery, 1))
    except (Exception, KeyboardInterrupt) as exc:
        fault_error(exc, "recovery_capture")
    finally:
        for runner in (recovery, first):
            if runner is not None and not runner.log._stream.closed:
                try:
                    finish(runner)
                except (Exception, KeyboardInterrupt) as exc:
                    fault_error(exc, "owned_run_cleanup")

    try:
        final_sources = source_provider()
        document = {"run_id": first.run_id if first else None,
            "experiment_started_at": first.started_at if first else None,
            "process_sample_max_age_s": process_sample_max_age_s,
            "expected_node_ids": (getattr(first, "server_identity", None) or {}).get("node_ids"),
            "captures": captures, "interim_observations": observations, "capture_errors": errors,
            "safety_violations": violations, "pause_attempted": attempted,
            "unpause_attempted": unpause_attempted, "unpause_confirmed": unpaused,
            "source_manifest": sources, "source_manifest_at_end": final_sources,
            "configuration_source": configuration_source, "observed_at": now()}
        result = evaluate("stale_process", document)
        result.update(evidence_file="fault_evidence.json", capture_errors=errors,
                      pause_attempted=attempted, unpause_confirmed=unpaused,
                      recovery_fixture_report="recovery-fixture.json",
                      archive_reports=["fault-run/report.json", "recovery-run/report.json"])
        if violations:
            result["status"] = "FAIL"
            result["checks"]["all_interim_observations_fail_closed"] = {"status": "FAIL", "observations": violations}
        if final_sources.get("source_sha256") != sources.get("source_sha256"):
            result["status"] = "FAIL"
            result["checks"]["source_unchanged"] = {"status": "FAIL", "detail": "Source changed during capture."}
        if errors and result["status"] not in {"FAIL", "INCOMPLETE"}:
            result["status"] = "INCOMPLETE"
        save_exclusive(config.output / "fault_evidence.json", document)
        save_exclusive(config.output / "fault_result.json", result)
        return result
    finally:
        log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-pause", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    parser.add_argument("--seed", type=int, default=123)
    args = parser.parse_args(argv)
    if not args.execute_pause:
        parser.error("--execute-pause is required before any experiment mutation")
    maximum_age, attribution = configured_sample_age(Path(".runtime/prosys-test/compose.yaml"))
    config = RunnerConfig(output=args.output, mode="short-dynamic", seed=args.seed,
                          commissioning_evidence=args.commissioning_evidence, telemetry_config=args.telemetry_config)
    result = run_stale_check(config, execute_pause=True, process_sample_max_age_s=maximum_age,
                            configuration_source=attribution)
    print(json.dumps({"status": result["status"], "fault_result": str(config.output / "fault_result.json")}, allow_nan=False))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
