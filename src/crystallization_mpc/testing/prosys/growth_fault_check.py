"""Explicit bounded real Synthetic G timeout/restart safety fixtures.

NOT a paper procedure: each owned run starts in sigma with adaptation disabled,
adds one 1 g seed, enables Synthetic G and then switches to G through normal
Central APIs. No paper stage gate is forced and real 120 s warmup is retained.
Each run has a 240 s setup deadline. Both runs and fault observation share at
most 600 s, including a final 60 s reserve for owned cleanup, not more sampling.
--execute-fault authorizes one
pause/unpause OR restart, End of the owned run, a guarded 42 C initialization,
and a distinct new run to prove recovery. No hardware/camera claim is made.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from .growth_fault_evidence import eligible_late_frames, evaluate_growth_fault, fresh_growth, is_hold
from .gsensor import SOURCE
from .restart_check import LABELS, PROJECT, run_command, save_exclusive
from .runner import EvidenceLog, ProsysRunner, RunnerConfig, RunnerError, epoch, finite, same_start, source_manifest, utc_now
from .stale_check import INSPECT_FORMAT, container_paused, container_running, prepare_recovery_fixture

CONTAINER = "mpcrystal-prosys_gsensor_1"
FIXTURE_LIMIT_S = 600
SETUP_LIMIT_S = 240
CLEANUP_RESERVE_S = 60


def _budgeted_ua_post(loop, coroutine, remaining: Callable, request_limit_s: float):
    """Bound each real UA coroutine, including the multi-step connect operation.

    Timeout is an uncertain operation, not permission to retry a write. Cancel
    the local pending coroutine and let commissioning record the failure.
    """
    try:
        timeout = min(request_limit_s, remaining())
    except BaseException:
        coroutine.close()
        raise
    future = asyncio.run_coroutine_threadsafe(coroutine, loop=loop)
    try:
        return future.result(timeout)
    except TimeoutError:
        future.cancel()
        raise


def _prepare_bounded_recovery(runner, path: Path, remaining: Callable):
    """Retain the guarded recovery contract without bypassing this fixture budget."""
    from asyncua.sync import Client
    from .opcua_client import ProsysProcessClient

    def ua_factory(endpoint, timeout_s):
        remaining()
        client = Client(endpoint, timeout=timeout_s, sync_wrapper_timeout=timeout_s + 1)
        # Every sync Client and Node operation shares this ThreadLoop. A single
        # guard around connect() would not bound its many underlying requests.
        client.tloop.post = lambda coroutine: _budgeted_ua_post(
            client.tloop.loop, coroutine, remaining, timeout_s + 1)
        return client

    proxy = SimpleNamespace(config=runner.config, server_identity=runner.server_identity,
        run_id=runner.run_id, started_at=runner.started_at,
        client=SimpleNamespace(request=lambda *args: runner._bounded_io(runner.client.request, *args)))
    return prepare_recovery_fixture(proxy, path,
        client_factory=lambda config: ProsysProcessClient(config, client_factory=ua_factory))


def inspect_gsensor(command_runner: Callable, target: str = CONTAINER) -> dict[str, Any]:
    raw = json.loads(command_runner(["podman", "inspect", "--type", "container", "--format", INSPECT_FORMAT, target], timeout=5))
    labels, state = raw.get("Labels") or {}, raw.get("State") or {}
    identifier, name = raw.get("Id"), raw.get("Name")
    if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier)
            or not isinstance(name, str) or name.lstrip("/") != CONTAINER):
        raise RunnerError("The exact isolated Synthetic G container was not identified.")
    for suffix, expected in (("project", PROJECT), ("service", "gsensor")):
        values = [labels[key] for key in LABELS if key.endswith("." + suffix) and labels.get(key)]
        if not values or any(value != expected for value in values):
            raise RunnerError("Synthetic G compose labels do not match the isolated stack.")
    if any(type(state.get(key)) is not bool for key in ("Running", "Paused")):
        raise RunnerError("Container running/paused flags are missing.")
    epoch(state.get("StartedAt"))
    return {"Id": identifier, "Name": name,
        "State": {key: state[key] for key in ("StartedAt", "Running", "Paused", "Status")},
        "Labels": {key: labels[key] for key in LABELS if labels.get(key)}}


class GrowthBus:
    """Read-only exclusive subscriber; never consumes the real Controller queue."""
    def __init__(self):
        self.connection = self.channel = None
        self.queue = "prosys.growth-fault." + uuid4().hex
    def subscribe(self):
        import pika
        params = pika.URLParameters("amqp://guest:guest@127.0.0.1:5674/%2F")
        params.connection_attempts, params.socket_timeout, params.stack_timeout = 1, 3, 5
        params.blocked_connection_timeout = 3
        self.connection = pika.BlockingConnection(params)
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange="idp.bus", passive=True)
        self.channel.queue_declare(queue=self.queue, durable=False, exclusive=True, auto_delete=True)
        self.channel.queue_bind(queue=self.queue, exchange="idp.bus", routing_key="gsensor.to.controller")
    def drain(self):
        result = []
        self.connection.process_data_events(time_limit=0)
        for _ in range(256):
            method, _, wire = self.channel.basic_get(self.queue, auto_ack=False)
            if method is None:
                return result
            self.channel.basic_ack(method.delivery_tag)
            result.append(wire)
        raise RunnerError("Unexpected excessive G traffic; bounded subscriber stopped.")
    def close(self):
        if self.connection is not None and self.connection.is_open:
            self.connection.close()  # exclusively owned auto-delete queue only


def run_growth_fault(config: RunnerConfig, *, mode: str, execute_fault: bool,
                     runner_factory: Callable = ProsysRunner, client=None,
                     command_runner: Callable = run_command, bus_factory: Callable = GrowthBus,
                     recovery_fixture: Callable = prepare_recovery_fixture,
                     monotonic: Callable = time.monotonic, sleep: Callable = time.sleep,
                     now: Callable = utc_now, source_provider: Callable = source_manifest) -> dict[str, Any]:
    if execute_fault is not True:
        raise ValueError("--execute-fault is required before any experiment or container mutation.")
    if config.mode != "fault-fixture" or config.fault_fixture_scope != "growth" or mode not in {"timeout", "restart"}:
        raise ValueError("Use an isolated fault-fixture and timeout/restart fault mode.")
    total_budget = min(FIXTURE_LIMIT_S, config.maximum_run_s)
    if total_budget <= CLEANUP_RESERVE_S:
        raise ValueError("Fault budget must leave time beyond the 60 s owned-cleanup reserve.")
    fixture_origin = monotonic()
    final_deadline = fixture_origin + total_budget
    active_deadline = final_deadline - CLEANUP_RESERVE_S
    log = EvidenceLog(config.output, now)
    sources = source_provider()
    captures, observations, raw_messages, errors, violations = [], [], [], [], []
    first = recovery = bus = before_container = None
    fault_attempted = unpause_attempted = unpaused = False
    first_finished = recovery_finished = False
    held = None
    cleanup_phase = False

    def remaining(*, cleanup=False):
        value = (final_deadline if cleanup else active_deadline) - monotonic()
        if value <= 0:
            raise TimeoutError("Shared G fault-fixture budget expired; no further test actions.")
        return value

    def bounded_command(argv, *, timeout):
        # Cleanup is the only allowed mutation after the active-work deadline.
        limit = remaining(cleanup=cleanup_phase)
        return command_runner(argv, timeout=min(timeout, limit))

    def capture(kind, phase, body):
        item = {"kind": kind, "phase": phase, "observed_at": now(), "body": deepcopy(body)}
        captures.append(item)
        log.append("growth_fault_capture", item)
        return item
    def error(exc, phase):
        item = {"phase": phase, "error_type": type(exc).__name__, "message": str(exc)}
        errors.append(item)
        log.append("growth_fault_error", item)
    def drain():
        for wire in bus.drain():
            text = wire.decode("utf-8")
            item = {"observed_at": now(), "wire_utf8": text, "sha256": hashlib.sha256(wire).hexdigest(), "message": json.loads(text)}
            raw_messages.append(item)
            log.append("raw_growth_message", item)
    def matching(runner, status):
        if status.get("current_run_id") != runner.run_id or not same_start(status.get("started_at"), runner.started_at):
            raise RunnerError("Controller identity changed; refusing another run.")
    def watchdog(runner):
        remaining()
        plant = runner.call("plant", "GET", "/status")
        log.append("growth_plant_watchdog", {"observed_at": now(), "body": deepcopy(plant)})
        try:
            okay = (plant.get("run_id") == runner.run_id
                and same_start(plant.get("experiment_started_at"), runner.started_at)
                and plant.get("running") is True and not plant.get("error")
                and finite(plant.get("sample_age_s")) and 0 <= plant["sample_age_s"] <= 5
                and 0 <= epoch(now()) - epoch(plant["heartbeat_at"]) <= 5)
        except (KeyError, TypeError, ValueError):
            okay = False
        if not okay:
            raise RunnerError("Independent owned plant heartbeat/identity failed; aborting fault fixture.")
    def producer(runner):
        value = runner.call("gsensor", "GET", "/status")
        if (value.get("current_run_id") != runner.run_id
                or not same_start(value.get("experiment_started_at"), runner.started_at)
                or value.get("source") != SOURCE or value.get("synthetic") is not True
                or value.get("timing") != {"sample_interval_s": 15, "warmup_s": 120}):
            raise RunnerError("Synthetic G identity/source or immutable 15/120 s timings do not match.")
        return value
    def read_controller(runner, phase):
        watchdog(runner)  # also checked before any potentially timed-out Controller GET
        try:
            status = runner.call("controller", "GET", "/api/status")
        finally:
            watchdog(runner)
        matching(runner, status)
        drain()
        item = {"observed_at": now(), "phase": phase, "body": deepcopy(status)}
        observations.append(item)
        log.append("growth_interim_controller", item)
        return status
    def setup_live(runner):
        deadline = min(monotonic() + SETUP_LIMIT_S, active_deadline)
        runner.operation_deadline = deadline
        runner.preflight()
        runner.prepare()
        watchdog(runner)
        producer(runner)
        response = runner.call("central", "POST", "/api/operation/controller/add-seed", {"expected_run_id": runner.run_id})
        event = response["event"]
        if event.get("run_id") != runner.run_id or not event.get("event_id"):
            raise RunnerError("Central seed event identity did not match.")
        seed_deadline = min(monotonic() + 45, deadline)
        while monotonic() < seed_deadline:
            remaining()
            runner.observe_once()
            watchdog(runner)
            drain()
            seeds = (runner.latest_status or {}).get("seed_events") or {}
            if seeds.get("count", 0) > 1:
                raise RunnerError("Unexpected additional seed event.")
            if seeds.get("count") == 1 and (seeds.get("last") or {}).get("event_id") == event["event_id"]:
                break
            sleep(1)
        else:
            raise RunnerError("Controller seed event was not acknowledged; never resend.")
        seeded = runner.call("plant", "POST", "/seed", {**runner.identity(), "event_id": event["event_id"], "mass_kg": .001})
        if seeded.get("seed_event_ids") != [event["event_id"]] or seeded.get("seed_mass_kg") != .001:
            raise RunnerError("Plant must acknowledge exactly one 1 g seed.")
        activation = runner.call("central", "GET", "/api/system/status")["gsensor"]["activation"]
        revision = activation.get("control_revision", activation.get("revision", 0))
        event_id = uuid4().hex
        activation_request = {"run_id": runner.run_id, "enabled": True,
            "expected_revision": revision, "event_id": event_id}
        requested_at = now()
        activation_response = runner.call("central", "POST", "/api/operation/gsensor/activation", activation_request)
        capture("activation", "before" if runner is first else "recovered", {
            "requested_at": requested_at, "request": activation_request, "response": activation_response})
        log.append("fault_fixture_activation", {**runner.identity(), "event_id": event_id,
            "scope": "Fault fixture only: sigma then G control, no adaptation, one 1 g seed, unchanged 120 s warmup."})
        frames = set()
        switch_result = None
        while monotonic() < deadline:
            remaining()
            runner.observe_once()  # only before injection or during the distinct NEW run
            watchdog(runner)
            drain()
            status = runner.latest_status or {}
            controls = (status.get("runtime_controls") or {}).get("configuration") or {}
            if controls.get("control_target") != ("G" if switch_result else "sigma") or controls.get("adaptation_enabled") is not False:
                raise RunnerError("Fault fixture target/adaptation differs from the explicit sigma-to-G setup.")
            if (status.get("scheduler") or {}).get("growth_dt_s") != 15 or (status.get("scheduler") or {}).get("controller_dt_s") != 15:
                raise RunnerError("Fault fixture requires unchanged 15 s control/G periods.")
            if fresh_growth(status, now()) and (status.get("growth_input") or {}).get("control_event_id") == event_id:
                frames.add(status["last_valid_sample"]["frame_seq"])
                if len(frames) >= 2:
                    if switch_result is None:
                        switch_result = runner.runtime_change({"control_target": "G"})
                        capture("runtime", "before" if runner is first else "recovered", {
                            "changes": {"control_target": "G"}, "result": switch_result,
                            "scope": "Explicit G-control fault fixture; not a paper phase transition."})
                        frames.clear()
                        drain()
                    elif ((status.get("last_control_output") or {}).get("tick_seq", 0) > switch_result["effective_tick"]
                            and (status["last_control_output"].get("runtime_configuration") or {}).get("control_target") == "G"):
                        producer(runner)
                        if monotonic() >= deadline:
                            raise TimeoutError("G baseline exceeded its actual 240 s setup deadline.")
                        runner.operation_deadline = active_deadline
                        return status
            sleep(min(1., config.poll_s))
        raise RunnerError("No two fresh accepted G frames and later verified output after actual warmup within 240 s.")
    def unpause(*, cleanup=False):
        nonlocal unpause_attempted, unpaused
        if mode != "timeout" or not fault_attempted or before_container is None or unpaused:
            return
        # Verify both run ownership and process identity before touching the paused container.
        if cleanup:
            first.operation_deadline = final_deadline
            remaining(cleanup=True)
            plant = first.call("plant", "GET", "/status")
            if plant.get("run_id") != first.run_id or not same_start(plant.get("experiment_started_at"), first.started_at):
                raise RunnerError("Plant ownership changed; cleanup will not unpause another run's G source.")
        else:
            watchdog(first)
        matching(first, first.call("controller", "GET", "/api/status"))
        current = inspect_gsensor(bounded_command, before_container["Id"])
        if current["Id"] != before_container["Id"] or current["State"]["StartedAt"] != before_container["State"]["StartedAt"]:
            raise RunnerError("Synthetic G was replaced/restarted; do not unpause another process.")
        if container_paused(current["State"]):
            if unpause_attempted:
                raise RunnerError("Uncertain unpause already attempted; manual intervention required.")
            unpause_attempted = True
            log.append("unpause_intent", {"container_id": current["Id"], "attempt": 1})
            try:
                bounded_command(["podman", "unpause", current["Id"]], timeout=10)
            except Exception as exc:
                log.append("unpause_uncertain", {"error_type": type(exc).__name__, "retried": False})
            current = inspect_gsensor(bounded_command, current["Id"])
        if (current["Id"] != before_container["Id"] or current["State"]["StartedAt"] != before_container["State"]["StartedAt"]
                or not container_running(current["State"])):
            raise RunnerError("Original Synthetic G was not confirmed running after single unpause.")
        unpaused = True
        capture("container", "resumed", current)
    def finish(runner, *, cleanup=False):
        runner.operation_deadline = final_deadline if cleanup else active_deadline
        try:
            runner.shutdown()
            if runner.shutdown_errors:
                raise RunnerError("Owned shutdown failed: " + "; ".join(runner.shutdown_errors))
            if runner is first and runner.run_id:
                capture("central", "ended", runner.call("central", "GET", f"/api/experiments/{runner.run_id}"))
        finally:
            try:
                runner.write_report()
            finally:
                runner.log.close()

    try:
        log.append("growth_fault_started", {"mode": mode, "source_manifest": sources,
            "budget": {"maximum_fixture_s": total_budget, "setup_per_run_s": SETUP_LIMIT_S,
                "cleanup_reserve_s": CLEANUP_RESERVE_S},
            "scope": "Synthetic-only fault fixture; paper phases/control-performance acceptance NOT_RUN."})
        first = runner_factory(replace(config, output=config.output / "fault-run"), client=client,
            monotonic=monotonic, sleep=sleep, now=now)
        bus = bus_factory()
        bus.subscribe()  # actual subscription precedes seed/activation and any fault
        setup_live(first)
        remaining()
        before_container = inspect_gsensor(bounded_command)
        capture("container", "before", before_container)
        if not container_running(before_container["State"]):
            raise RunnerError("Synthetic G must initially be running and not paused.")
        baseline = read_controller(first, "before")
        if not fresh_growth(baseline, now()):
            raise RunnerError("Fresh accepted G baseline was lost before injection.")
        capture("controller", "before", baseline)
        capture("gsensor", "before", producer(first))
        remaining()
        fault_attempted = True
        command = ["podman", "pause", before_container["Id"]] if mode == "timeout" else ["podman", "restart", "--time", "10", before_container["Id"]]
        log.append("fault_intent", {"mode": mode, "container_id": before_container["Id"], "attempt": 1})
        try:
            bounded_command(command, timeout=10 if mode == "timeout" else 30)
        except Exception as exc:
            log.append("fault_outcome_uncertain", {"error_type": type(exc).__name__, "retried": False})
        fault_container = inspect_gsensor(bounded_command, before_container["Id"])
        if fault_container["Id"] != before_container["Id"]:
            raise RunnerError("Container identity changed during fault injection.")
        if mode == "timeout":
            if not container_paused(fault_container["State"]) or fault_container["State"]["StartedAt"] != before_container["State"]["StartedAt"]:
                raise RunnerError("Single pause was not confirmed on the original process.")
        elif (not container_running(fault_container["State"])
                or epoch(fault_container["State"]["StartedAt"]) <= epoch(before_container["State"]["StartedAt"])):
            raise RunnerError("Single restart was not confirmed with a later process StartedAt.")
        capture("container", "fault", fault_container)
        reason = "growth_timeout" if mode == "timeout" else "gsensor_error"
        deadline = min(monotonic() + 90, active_deadline)
        while monotonic() < deadline:
            status = read_controller(first, "fault")
            if is_hold(status, reason):
                held = capture("controller", "held", status)
                break
            if status.get("status") not in {"running", "recovery_required"}:
                raise RunnerError("Controller ended in another error, not the required G safety hold.")
            if status.get("status") == "recovery_required":
                raise RunnerError("Safety hold reason/flags differ from the requested fault case.")
            sleep(min(1., config.poll_s))
        if held is None:
            violations.append({"reason": "required_G_safety_hold_not_observed", "observed_at": now()})
            raise RunnerError("Required manual-confirmation G hold was absent after 90 s.")
        if mode == "timeout":
            unpause()
        else:
            capture("container", "resumed", inspect_gsensor(bounded_command, before_container["Id"]))
        resumed = next(item for item in captures if item["kind"] == "container" and item["phase"] == "resumed")
        deadline, evidence_at = min(monotonic() + 90, active_deadline), None
        eligible_observed_at = None
        while monotonic() < deadline:
            status = read_controller(first, "held_observation")
            if (not is_hold(status, reason) or status.get("last_control_output") != held["body"].get("last_control_output")
                    or status.get("scheduler", {}).get("tick_count") != held["body"].get("scheduler", {}).get("tick_count")):
                violations.append({"reason": "old_run_unlocked_or_advanced", "observed_at": now(), "body": deepcopy(status)})
                raise RunnerError("Old Controller run advanced/unlocked without End; stop immediately.")
            gs = producer(first)
            if mode == "timeout":
                late = eligible_late_frames(raw_messages, baseline, resumed["observed_at"], now())
                if late and evidence_at is None:
                    qualifying = min(late, key=lambda item: epoch(item["observed_at"]))
                    eligible_observed_at = qualifying["observed_at"]
                    evidence_at = monotonic()
                    log.append("eligible_late_frame_observation_started", {
                        "resumed_inspection_at": resumed["observed_at"], "observed_at": eligible_observed_at,
                        "frame_seq": qualifying["message"]["payload"]["frame_seq"], "required_observation_s": 31})
            elif (gs.get("experiment_lifecycle_status") == "error" and gs.get("recovery", {}).get("requires_new_run") is True
                    and gs.get("active") is False and gs.get("measurement_ready") is False):
                if evidence_at is None:
                    evidence_at = monotonic()
            if (evidence_at is not None and monotonic() - evidence_at >= 31
                    and (mode != "timeout" or epoch(now()) - epoch(eligible_observed_at) >= 31)):
                # Fetch a new snapshot after the full interval. Do not relabel
                # the status read earlier in this iteration as a later sample.
                status = read_controller(first, "held_final_observation")
                if (not is_hold(status, reason) or status.get("last_control_output") != held["body"].get("last_control_output")
                        or status.get("scheduler", {}).get("tick_count") != held["body"].get("scheduler", {}).get("tick_count")):
                    violations.append({"reason": "old_run_unlocked_or_advanced", "observed_at": now(), "body": deepcopy(status)})
                    raise RunnerError("Old Controller run advanced/unlocked at the final observation boundary.")
                gs = producer(first)
                capture("controller", "after", status)
                capture("gsensor", "after", gs)
                break
            sleep(min(1., config.poll_s))
        else:
            raise RunnerError("Restored-input/terminal-producer observation did not complete within 90 s.")
        finish(first)
        first_finished = True
        remaining()
        fixture = (_prepare_bounded_recovery(first, config.output / "recovery-fixture.json", remaining)
            if recovery_fixture is prepare_recovery_fixture
            else recovery_fixture(first, config.output / "recovery-fixture.json"))
        remaining()
        if fixture.get("action_status") != "PASS":
            raise RunnerError("Explicit new-run recovery initialization did not pass.")
        log.append("recovery_fixture", {"action_status": "PASS", "path": "recovery-fixture.json"})
        recovery = runner_factory(replace(config, output=config.output / "recovery-run"), client=client,
            monotonic=monotonic, sleep=sleep, now=now)
        status = setup_live(recovery)
        if recovery.run_id == first.run_id or epoch(recovery.started_at) <= epoch(first.started_at):
            raise RunnerError("Recovery did not create a distinct later experiment.")
        capture("controller", "recovered", status)
        capture("gsensor", "recovered", producer(recovery))
    except (Exception, KeyboardInterrupt) as exc:
        error(exc, "capture")
    finally:
        cleanup_phase = True
        try:
            unpause(cleanup=True)
        except (Exception, KeyboardInterrupt) as exc:
            error(exc, "unpause_cleanup")
        for runner in (recovery, first):
            if runner is not None and not runner.log._stream.closed:
                try:
                    finish(runner, cleanup=True)
                    if runner is first:
                        first_finished = True
                    else:
                        recovery_finished = True
                except (Exception, KeyboardInterrupt) as exc:
                    error(exc, "owned_shutdown")
        if bus is not None:
            try:
                drain()
            except Exception as exc:
                error(exc, "subscriber_cleanup")
            finally:
                try:
                    bus.close()
                except Exception as exc:
                    error(exc, "subscriber_close")
    try:
        if monotonic() > final_deadline:
            error(TimeoutError("Total G fault-fixture budget exceeded; no timing PASS."), "total_budget")
        document = {"mode": mode, "run_id": first.run_id if first else None,
            "experiment_started_at": first.started_at if first else None,
            "captures": captures, "interim_observations": observations, "raw_messages": raw_messages,
            "capture_errors": errors, "safety_violations": violations,
            "fault_attempted": fault_attempted, "unpause_attempted": unpause_attempted,
            "budget": {"maximum_fixture_s": total_budget, "setup_per_run_s": SETUP_LIMIT_S,
                "cleanup_reserve_s": CLEANUP_RESERVE_S, "elapsed_s": monotonic() - fixture_origin},
            "source_manifest": sources, "source_manifest_at_end": source_provider(),
            "cleanup_complete": first_finished and recovery_finished,
            "archive_statuses": [(getattr(r, "archive_validation", None) or {}).get("status") for r in (first, recovery)],
            "scope": "Fault fixture only: sigma then G control, no adaptation; one seed per run; camera/hardware and paper phases NOT_RUN."}
        result = evaluate_growth_fault(document)
        result.update(evidence_file="fault_evidence.json", archive_reports=["fault-run/report.json", "recovery-run/report.json"])
        save_exclusive(config.output / "fault_evidence.json", document)
        save_exclusive(config.output / "fault_result.json", result)
        return result
    finally:
        log.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("timeout", "restart"), required=True)
    parser.add_argument("--execute-fault", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    parser.add_argument("--seed", type=int, default=123)
    args = parser.parse_args(argv)
    if not args.execute_fault:
        parser.error("--execute-fault is required before any experiment/container mutation")
    config = RunnerConfig(output=args.output, mode="fault-fixture", seed=args.seed,
        commissioning_evidence=args.commissioning_evidence, telemetry_config=args.telemetry_config)
    result = run_growth_fault(config, mode=args.mode, execute_fault=True)
    print(json.dumps({"status": result["status"], "fault_result": str(config.output / "fault_result.json")}))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
