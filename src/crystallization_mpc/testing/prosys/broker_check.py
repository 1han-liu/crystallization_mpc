"""Explicit isolated RabbitMQ availability test, not in-flight Writer proof.

--execute-stop starts two owned short experiments and stops/starts only the
verified test broker once. It proves Controller fail-closed behavior, restores
fresh input, observes no old-run resume, then ends that run and performs guarded
42 C initialization for a new recovery run. No Writer result is manufactured.
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

from .fault_evidence import evaluate
from .restart_check import LABELS, PROJECT, configured_sample_age, run_command, save_exclusive
from .runner import EvidenceLog, ProsysRunner, RunnerConfig, RunnerError, epoch, finite, same_start, source_manifest, utc_now
from .stale_check import container_running, prepare_recovery_fixture


CONTAINER = "mpcrystal-prosys_rabbitmq_1"
INSPECT_FORMAT = ('{"Id":{{json .ID}},"Name":{{json .Name}},"State":{'
    '"StartedAt":{{json .State.StartedAt}},"FinishedAt":{{json .State.FinishedAt}},'
    '"Running":{{json .State.Running}},"Paused":{{json .State.Paused}},"Status":{{json .State.Status}}},"Labels":{' +
    ",".join(json.dumps(key) + ':{{json (index .Config.Labels ' + json.dumps(key) + ')}}'
             for key in LABELS) + '}}')


def inspect_broker(command_runner: Callable, target: str = CONTAINER, *, observer: Callable | None = None) -> dict[str, Any]:
    raw = json.loads(command_runner(["podman", "inspect", "--type", "container", "--format", INSPECT_FORMAT, target], timeout=5))
    if not isinstance(raw, dict):
        raise RunnerError("Broker inspection must return one object.")
    labels, state = raw.get("Labels"), raw.get("State")
    clean = {"Id": raw.get("Id"), "Name": raw.get("Name"),
        "State": {key: state.get(key) for key in ("StartedAt", "FinishedAt", "Running", "Paused", "Status")} if isinstance(state, dict) else None,
        "Labels": {key: labels.get(key) for key in LABELS if labels.get(key)} if isinstance(labels, dict) else None}
    if observer:
        observer(deepcopy(clean))
    if (not isinstance(clean["Id"], str) or not re.fullmatch(r"[0-9a-f]{64}", clean["Id"])
            or str(clean["Name"]).lstrip("/") != CONTAINER or not isinstance(labels, dict) or not isinstance(state, dict)):
        raise RunnerError("The exact isolated RabbitMQ container could not be identified.")
    for suffix, expected in (("project", PROJECT), ("service", "rabbitmq")):
        values = [labels[key] for key in LABELS if key.endswith("." + suffix) and labels.get(key)]
        if not values or any(value != expected for value in values):
            raise RunnerError("Broker project/service labels differ from the isolated stack.")
    if type(state.get("Running")) is not bool or type(state.get("Paused")) is not bool:
        raise RunnerError("Broker running/paused flags are missing or invalid.")
    epoch(state.get("StartedAt"))
    return clean


def container_stopped(state: Mapping[str, Any]) -> bool:
    return state.get("Status") == "exited" and state.get("Running") is False and state.get("Paused") is False


def run_broker_check(config: RunnerConfig, *, execute_stop: bool, process_sample_max_age_s: float,
                     configuration_source: Mapping[str, Any] | None = None,
                     runner_factory: Callable = ProsysRunner, client: Any = None,
                     command_runner: Callable = run_command, monotonic: Callable = time.monotonic,
                     sleep: Callable = time.sleep, now: Callable = utc_now,
                     source_provider: Callable = source_manifest,
                     recovery_fixture: Callable = prepare_recovery_fixture) -> dict[str, Any]:
    if execute_stop is not True:
        raise ValueError("Explicit --execute-stop is required before any experiment mutation.")
    if config.mode != "short-dynamic" or not finite(process_sample_max_age_s) or not 0 < process_sample_max_age_s <= 30:
        raise ValueError("Broker capture requires short-dynamic mode and the actual bounded process sample age.")
    log = EvidenceLog(config.output, now)
    captures, observations, recovery_observations, errors, violations, boundary_uncertainties = [], [], [], [], [], []
    first = recovery = None
    attempted = start_attempted = restored = False
    identifier = before_container = failed = None
    sources = source_provider()

    def capture(kind, phase, body):
        item = {"kind": kind, "phase": phase, "observed_at": now(), "body": deepcopy(body)}
        captures.append(item)
        log.append("broker_capture", item)
        return item

    def error(exc, stage):
        item = {"stage": stage, "type": type(exc).__name__, "message": str(exc)}
        errors.append(item)
        log.append("broker_capture_error", item)

    def inspect(phase, target=CONTAINER):
        return inspect_broker(command_runner, target, observer=lambda body: log.append(
            "broker_inspection", {"phase": phase, "observed_at": now(), "body": body}))

    def matching(runner, status):
        if status.get("current_run_id") != runner.run_id or not same_start(status.get("started_at"), runner.started_at):
            raise RunnerError("Controller no longer belongs to this owned run.")

    def plant_watchdog(runner):
        plant = runner.call("plant", "GET", "/status")
        log.append("plant_watchdog", {"observed_at": now(), "body": deepcopy(plant)})
        try:
            healthy = (plant.get("run_id") == runner.run_id and same_start(plant.get("experiment_started_at"), runner.started_at)
                and plant.get("running") is True and not plant.get("error")
                and finite(plant.get("sample_age_s")) and 0 <= plant["sample_age_s"] <= 5
                and 0 <= epoch(now()) - epoch(plant["heartbeat_at"]) <= 5)
        except (KeyError, TypeError, ValueError):
            healthy = False
        if not healthy:
            raise RunnerError("Owned external plant must remain running with a fresh OPC UA heartbeat.")

    def healthy(runner, status):
        process = (status.get("integrations") or {}).get("process") or {}
        output = status.get("last_control_output") or {}
        try:
            matching(runner, status)
            return (status.get("active") is True and status.get("status") == "running"
                and process.get("connected") is True and process.get("sample_valid") is True and process.get("missing_fields") == []
                and finite(process.get("sample_age_s")) and 0 <= process["sample_age_s"] <= process_sample_max_age_s
                and 0 <= epoch(now()) - epoch(process["last_read_at"]) <= process_sample_max_age_s
                and output.get("run_id") == runner.run_id and type(output.get("tick_seq")) is int and output["tick_seq"] > 0
                and (output.get("result") or {}).get("valid") is True and output.get("process_write_error") is None
                and (output.get("process_write") or {}).get("verified") is True
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
            sleep(min(config.poll_s, 1.))
        raise RunnerError("Required distinct fresh verified outputs were not observed within 90 seconds.")

    def restore_broker():
        nonlocal start_attempted, restored
        if not attempted or identifier is None:
            return
        observed = inspect("before_cleanup", identifier)
        if observed["Id"] != identifier or observed["State"]["StartedAt"] != before_container["State"]["StartedAt"]:
            raise RunnerError("Broker identity/process changed; refusing to start a replacement.")
        if not container_stopped(observed["State"]):
            if container_running(observed["State"]):
                # Stop was not observed: do not blindly start/restart a running broker.
                log.append("broker_cleanup_no_stop", {"observed_at": now(), "body": observed})
                return
            raise RunnerError("Broker is paused or unknown; cleanup cannot infer a safe start action.")
        start_attempted = True
        log.append("broker_start_intent", {"container_id": identifier, "attempt": 1})
        try:
            command_runner(["podman", "start", identifier], timeout=20)
        except Exception as exc:
            log.append("broker_start_uncertain", {"type": type(exc).__name__, "retried": False})
        deadline = monotonic() + 60
        new_process_start = None
        while monotonic() < deadline:
            observed = inspect("after_start", identifier)
            if observed["Id"] != identifier:
                raise RunnerError("Broker container changed after start; no replacement is targeted.")
            if container_running(observed["State"]) and epoch(observed["State"]["StartedAt"]) > epoch(before_container["State"]["StartedAt"]):
                if new_process_start is None:
                    new_process_start = observed["State"]["StartedAt"]
                elif observed["State"]["StartedAt"] != new_process_start:
                    raise RunnerError("Broker restarted again after the one authorized start; no further actions are permitted.")
                try:
                    command_runner(["podman", "exec", identifier, "rabbitmq-diagnostics", "-q", "check_running"], timeout=5)
                except Exception as exc:
                    log.append("broker_readiness_pending", {"observed_at": now(), "type": type(exc).__name__})
                else:
                    confirmed = inspect("after_readiness_check", identifier)
                    if (confirmed["Id"] != identifier
                            or confirmed["State"]["StartedAt"] != observed["State"]["StartedAt"]
                            or not container_running(confirmed["State"])):
                        raise RunnerError("Broker process changed during its readiness check; recovery is unconfirmed.")
                    restored = True
                    capture("broker", "restored", confirmed)
                    return
            if first is not None:
                plant_watchdog(first)
            sleep(1)
        raise RunnerError("The single broker start attempt did not become healthy; manual attention is required.")

    def finish(runner):
        if runner is None:
            return
        try:
            runner.shutdown()
            if runner.shutdown_errors:
                raise RunnerError("Owned-run cleanup failed: " + "; ".join(runner.shutdown_errors))
        finally:
            try:
                runner.write_report()
            finally:
                runner.log.close()

    try:
        first = runner_factory(replace(config, output=config.output / "fault-run"), client=client,
                               monotonic=monotonic, sleep=sleep, now=now)
        log.append("broker_check_started", {"source_manifest": sources, "configuration_source": configuration_source,
            "scope": "Broker availability fail-closed; in-flight command outcome NOT RUN"})
        first.preflight()
        first.prepare()
        wait_ticks(first, 2)
        before_container = inspect("before_stop")
        capture("broker", "before", before_container)
        if not container_running(before_container["State"]):
            raise RunnerError("Only a running, unpaused isolated RabbitMQ container may be stopped.")
        before = first.call("controller", "GET", "/api/status")
        if not healthy(first, before):
            raise RunnerError("Controller input lost health before the planned broker stop.")
        capture("controller", "before", before)
        dt = (before.get("scheduler") or {}).get("controller_dt_s")
        if not finite(dt) or not 0 < dt <= 30:
            raise RunnerError("Configured control period exceeds this bounded fault test.")
        identifier, attempted = before_container["Id"], True
        log.append("broker_stop_intent", {"container_id": identifier, "attempt": 1})
        try:
            command_runner(["podman", "stop", "--time", "10", identifier], timeout=20)
        except Exception as exc:
            log.append("broker_stop_uncertain", {"type": type(exc).__name__, "retried": False})
        observed = inspect("after_stop", identifier)
        if (observed["Id"] != identifier or observed["State"]["StartedAt"] != before_container["State"]["StartedAt"]
                or not container_stopped(observed["State"])):
            raise RunnerError("The single broker stop attempt was not confirmed on its original container.")
        epoch(observed["State"]["FinishedAt"])
        broker_exit = epoch(observed["State"]["FinishedAt"])
        capture("broker", "stopped", observed)
        deadline, disconnected = monotonic() + dt + 30, False
        while monotonic() < deadline:
            plant_watchdog(first)
            try:
                status = first.call("controller", "GET", "/api/status")
            except Exception as exc:
                # Controller holds its lock while a process read times out.
                # HTTP unavailability is not an invented terminal status.
                log.append("broker_controller_status_unavailable", {"observed_at": now(), "type": type(exc).__name__})
                plant_watchdog(first)
                sleep(.2)
                continue
            matching(first, status)
            item = {"observed_at": now(), "body": deepcopy(status)}
            observations.append(item)
            log.append("broker_interim_status", item)
            process = (status.get("integrations") or {}).get("process") or {}
            if not disconnected and process.get("connected") is False and process.get("sample_valid") is False:
                capture("controller", "disconnected", status)
                disconnected = True
            output = status.get("last_control_output") or {}
            if output != before.get("last_control_output") and (output.get("result") or {}).get("valid") is True:
                write = output.get("process_write") or {}
                try:
                    completed_before_exit = (write.get("verified") is True
                        and epoch(output["computed_at"]) <= broker_exit and epoch(write["written_at"]) <= broker_exit)
                except (KeyError, TypeError, ValueError):
                    completed_before_exit = False
                if completed_before_exit:
                    log.append("broker_cycle_completed_before_exit", item)
                else:
                    boundary_uncertainties.append({"reason": "success_confirmation_time_unproven_at_broker_exit", **item})
            if status.get("status") == "error" and status.get("active") is False:
                failed = capture("controller", "fault", status)
                break
            sleep(.2)
        else:
            violations.append({"reason": "no_terminal_stop_after_broker_loss", "observed_at": now()})
            raise RunnerError("Controller did not fail closed during the bounded broker outage.")
    except (Exception, KeyboardInterrupt) as exc:
        error(exc, "fault_capture")
    finally:
        try:
            restore_broker()
        except (Exception, KeyboardInterrupt) as exc:
            error(exc, "broker_restore")

    try:
        if first and failed and restored and not errors:
            plant_watchdog(first)
            after_start = next(item for item in captures if item["kind"] == "broker" and item["phase"] == "restored")
            first._probe_fresh_telegraf(after_start["observed_at"])
            plant_watchdog(first)
            capture("telegraf", "recovered_input", first.startup_telegraf_readiness)
            deadline = monotonic() + 2 * dt + 1
            while True:
                plant_watchdog(first)
                status = first.call("controller", "GET", "/api/status")
                matching(first, status)
                item = {"observed_at": now(), "body": deepcopy(status)}
                recovery_observations.append(item)
                log.append("broker_recovery_observation", item)
                if (status.get("active") is not False or status.get("status") != "error"
                        or status.get("last_control_output") != failed["body"].get("last_control_output")):
                    violations.append({"reason": "old_run_resumed", **item})
                if monotonic() >= deadline:
                    capture("controller", "after", status)
                    break
                sleep(min(config.poll_s, 1.))
            finish(first)
            fixture = recovery_fixture(first, config.output / "recovery-fixture.json")
            if fixture.get("action_status") != "PASS":
                raise RunnerError("Guarded recovery fixture did not pass.")
            recovery = runner_factory(replace(config, output=config.output / "recovery-run"), client=client,
                                      monotonic=monotonic, sleep=sleep, now=now)
            recovery.preflight()
            recovery.prepare()
            if recovery.run_id == first.run_id or epoch(recovery.started_at) <= epoch(first.started_at):
                raise RunnerError("Recovery must be a new later owned run.")
            capture("controller", "recovered", wait_ticks(recovery, 1))
    except (Exception, KeyboardInterrupt) as exc:
        error(exc, "recovery_capture")
    finally:
        for runner in (recovery, first):
            if runner is not None and not runner.log._stream.closed:
                try:
                    finish(runner)
                except (Exception, KeyboardInterrupt) as exc:
                    error(exc, "owned_run_cleanup")

    try:
        final_sources = source_provider()
        document = {"run_id": first.run_id if first else None, "experiment_started_at": first.started_at if first else None,
            "process_sample_max_age_s": process_sample_max_age_s,
            "expected_node_ids": (getattr(first, "server_identity", None) or {}).get("node_ids"),
            "captures": captures, "interim_observations": observations, "recovery_observations": recovery_observations,
            "capture_errors": errors, "safety_violations": violations, "stop_attempted": attempted,
            "exit_boundary_uncertainties": boundary_uncertainties,
            "start_attempted": start_attempted, "broker_restored": restored,
            "source_manifest": sources, "source_manifest_at_end": final_sources,
            "configuration_source": configuration_source, "observed_at": now(),
            "in_flight_writer_outcome": "NOT RUN: broker availability alone does not prove an interrupted command outcome"}
        result = evaluate("broker_availability", document)
        result.update(evidence_file="fault_evidence.json", capture_errors=errors, stop_attempted=attempted,
                      broker_restored=restored, in_flight_writer_outcome=document["in_flight_writer_outcome"],
                      recovery_fixture_report="recovery-fixture.json", archive_reports=["fault-run/report.json", "recovery-run/report.json"])
        if violations:
            result["status"] = "FAIL"
            result["checks"]["all_observed_cycles_fail_closed"] = {"status": "FAIL", "observations": violations}
        if final_sources.get("source_sha256") != sources.get("source_sha256"):
            result["status"] = "FAIL"
            result["checks"]["source_unchanged"] = {"status": "FAIL", "detail": "Source changed during capture."}
        if (errors or boundary_uncertainties) and result["status"] not in {"FAIL", "INCOMPLETE"}:
            result["status"] = "INCOMPLETE"
            if boundary_uncertainties:
                result["reason"] = "A valid cycle crosses the graceful broker-exit boundary; actual confirmation timing is unproven."
        save_exclusive(config.output / "fault_evidence.json", document)
        save_exclusive(config.output / "fault_result.json", result)
        return result
    finally:
        log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-stop", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    parser.add_argument("--seed", type=int, default=123)
    args = parser.parse_args(argv)
    if not args.execute_stop:
        parser.error("--execute-stop is required before any experiment mutation")
    maximum_age, attribution = configured_sample_age(Path(".runtime/prosys-test/compose.yaml"))
    config = RunnerConfig(output=args.output, mode="short-dynamic", seed=args.seed,
        commissioning_evidence=args.commissioning_evidence, telemetry_config=args.telemetry_config)
    result = run_broker_check(config, execute_stop=True, process_sample_max_age_s=maximum_age,
                             configuration_source=attribution)
    print(json.dumps({"status": result["status"], "fault_result": str(config.output / "fault_result.json")}, allow_nan=False))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
