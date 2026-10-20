"""Opt-in host test of supervisory shutdown after the owned model stops.

The harness does not start, stop or reconfigure native Prosys or containers.
One explicit old-model /stop is the fault. The normal runner must reject that
model even while Telegraf still reports fresh reads of its frozen values.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable

from crystallization_mpc.testing.prosys.restart_check import save_exclusive
from crystallization_mpc.testing.prosys.runner import (
    EvidenceLog, HttpReadTimeout, JsonHttpClient, ProsysRunner, RunnerConfig, RunnerError,
    epoch, finite, same_start, source_manifest, utc_now, verified_write,
)
from crystallization_mpc.testing.prosys.raw_store import timestamp_ns
from crystallization_mpc.testing.prosys.stale_check import prepare_recovery_fixture


class ModelStopSafetyFailure(RunnerError):
    pass


class BoundedHttpClient:
    """Share one real deadline without retrying an unknown HTTP mutation."""
    def __init__(self, base, deadline: float, monotonic: Callable = time.monotonic):
        self.base, self.deadline, self.monotonic = base, deadline, monotonic

    def _call(self, operation, *args):
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("Model-stop test deadline expired before I/O.")
        previous = getattr(self.base, "timeout_s", None)
        if previous is not None:
            self.base.timeout_s = min(previous, remaining)
        try:
            result = operation(*args)
            if self.monotonic() >= self.deadline:
                raise TimeoutError("Model-stop deadline expired during I/O; outcome is not retried.")
            return result
        finally:
            if previous is not None:
                self.base.timeout_s = previous

    def request(self, method, url, payload=None):
        return self._call(self.base.request, method, url, payload)

    def get_bytes(self, url):
        return self._call(self.base.get_bytes, url)


def verify_frozen_rounds(proof: dict, node_ids: dict, stopped_at: str) -> dict:
    try:
        return _verify_frozen_rounds(proof, node_ids, stopped_at)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise RunnerError("Frozen-value proof has malformed identity, timestamp or measurement fields.") from exc


def _verify_frozen_rounds(proof: dict, node_ids: dict, stopped_at: str) -> dict:
    if (proof.get("status") != "PASS" or proof.get("node_ids") != node_ids
            or set(node_ids) != {"T", "T_j", "c", "count_middle", "T_j_set"}):
        raise RunnerError("Frozen-value proof lacks the exact verified five-node read path.")
    rounds = proof.get("rounds")
    if not isinstance(rounds, list) or len(rounds) != 3:
        raise RunnerError("Exactly three actual fresh collections are required.")
    completed, cutoff = timestamp_ns(proof["completed_at"]), timestamp_ns(stopped_at)
    stamps = []
    for row in rounds:
        stamp = timestamp_ns(row["read_at"])
        if (type(row.get("collection_timestamp_ns")) is not int or row["collection_timestamp_ns"] != stamp
                or stamp <= cutoff or not 0 <= completed - stamp <= 5_000_000_000
                or not all(finite(row.get(key)) for key in node_ids)
                or any((row.get("status_codes") or {}).get(key) not in
                       {"OK (0x0)", "The operation succeeded. StatusGood (0x0)"} for key in node_ids)
                or any(type(row.get(key)) is not int or row[key] < 0 for key in ("invalid_message_count", "reconnect_count"))):
            raise RunnerError("Frozen-value proof contains stale, incomplete, nonfinite or bad-quality input.")
        stamps.append(stamp)
    if (not all(0 < right - left <= 2_500_000_000 for left, right in zip(stamps, stamps[1:]))
            or any(row[key] != rounds[0][key] for row in rounds for key in
                   ("T", "T_j", "c", "count_middle", "invalid_message_count", "reconnect_count"))):
        raise RunnerError("Fresh collections must retain frozen physics with no invalid/reconnect increments.")
    return {"status": "PASS", "round_count": 3, "collection_timestamp_ns": stamps,
        "frozen_physics": {key: rounds[0][key] for key in ("T", "T_j", "c", "count_middle")},
        "setpoint_values": [row["T_j_set"] for row in rounds],
        "scope": "Fresh Telegraf collection times with unchanged model-written values; not fresh physical evolution."}


def check_command_archive(directory: Path, run_id: str, started_at: str) -> dict:
    try:
        return _check_command_archive(directory, run_id, started_at)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise RunnerError("Command archive contains malformed identities, timestamps or payloads.") from exc


def _check_command_archive(directory: Path, run_id: str, started_at: str) -> dict:
    with (directory / "controller_data.csv").open(encoding="utf-8-sig", newline="") as stream:
        controllers = list(csv.DictReader(stream))
    with (directory / "events.csv").open(encoding="utf-8-sig", newline="") as stream:
        events = list(csv.DictReader(stream))
    if not controllers:
        raise RunnerError("Model-stop run has no archived Controller attempts.")
    seen_ticks, command_ids = set(), set()
    for row in controllers:
        tick = int(row["tick_seq"])
        if (row.get("run_id") != run_id or tick in seen_ticks or tick < 1
                or row.get("valid", "").lower() != "true" or row.get("process_write_attempted", "").lower() != "true"):
            raise RunnerError("Archive contains an invalid, foreign or duplicate control attempt.")
        seen_ticks.add(tick)
        matching = [item for item in events if item.get("run_id") == run_id and item.get("tick_seq") == str(tick)]
        prepared = [item for item in matching if item.get("service") == "controller" and item.get("stage") == "prepared"]
        if len(prepared) != 1 or not prepared[0].get("command_id") or prepared[0]["command_id"] in command_ids:
            raise RunnerError("A Controller tick lacks its unique prepared production command.")
        command_id = prepared[0]["command_id"]
        command_ids.add(command_id)
        payload = json.loads(prepared[0]["payload"])
        command = {**payload}
        if (command.get("command_id") != command_id or command.get("run_id") != run_id
                or command.get("tick_seq") != tick or not same_start(command.get("run_started_at"), started_at)
                or not finite(command.get("setpoint_K")) or not finite(float(row["T_j_set"]))
                or abs(command["setpoint_K"] - float(row["T_j_set"])) > 1e-8):
            raise RunnerError("Archived command payload does not match its Controller tick/start/setpoint.")
        receipts = []
        for service, stage in (("controller", "receipt"), ("opcua_writer", "result")):
            rows = [item for item in matching if item.get("service") == service and item.get("stage") == stage
                    and item.get("command_id") == command_id]
            if len(rows) != 1 or rows[0].get("status") != "success":
                raise RunnerError("A written tick lacks exactly one successful Controller/Writer audit receipt.")
            receipt = json.loads(rows[0]["payload"])
            if receipt.get("error") is not None or not verified_write({"command": command, "receipt": receipt, "write": receipt.get("write_result")},
                                  run_id=run_id, started_at=started_at):
                raise RunnerError("Archived production write receipt has inconsistent identity/readback.")
            receipts.append(receipt)
        if receipts[0] != receipts[1]:
            raise RunnerError("Controller and Writer archived different command outcomes.")
    if seen_ticks != set(range(1, max(seen_ticks) + 1)):
        raise RunnerError("Archived Controller attempts are not contiguous.")
    if any(item.get("command_id") not in command_ids for item in events
           if item.get("run_id") == run_id and (item.get("service"), item.get("stage")) in
           {("controller", "prepared"), ("controller", "receipt"), ("opcua_writer", "result")}):
        raise RunnerError("Command archive contains an orphan command with no Controller attempt.")
    return {"status": "PASS", "ticks": sorted(seen_ticks), "command_ids": sorted(command_ids)}


def run_model_stop_check(config: RunnerConfig, *, execute_stop: bool, runner_factory=ProsysRunner,
                         client=None, monotonic=time.monotonic, sleep=time.sleep, now=utc_now,
                         source_provider=source_manifest, recovery_fixture=prepare_recovery_fixture,
                         total_timeout_s: float = 180) -> dict:
    if execute_stop is not True:
        raise ValueError("Explicit --execute-stop is required before creating an experiment.")
    if config.mode != "short-dynamic" or not finite(total_timeout_s) or not 60 <= total_timeout_s <= 180:
        raise ValueError("Model-stop capture requires short-dynamic and a total budget of 60..180 seconds.")
    began, source_before = monotonic(), source_provider()
    final_deadline = began + total_timeout_s
    http = BoundedHttpClient(client or JsonHttpClient(timeout_s=5), final_deadline - 20, monotonic)
    log = EvidenceLog(config.output, now)
    captures, errors, checks, runners = [], [], {}, []
    ended, end_attempted, reports, normal_plant_stop_attempted = {}, set(), set(), set()
    old = recovery = None
    stop_attempted = False
    harness_before = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def capture(kind, body):
        record = {"kind": kind, "observed_at": now(), "body": deepcopy(body)}
        captures.append(record)
        log.append(kind, record)
        return record

    def fail(exc, phase):
        item = {"phase": phase, "type": type(exc).__name__, "message": str(exc),
            "unsafe_observation": isinstance(exc, ModelStopSafetyFailure)}
        errors.append(item)
        log.append("capture_error", item)

    def remaining(reserve=0):
        if http.deadline - monotonic() <= reserve:
            raise TimeoutError("Insufficient remaining model-stop budget; no new mutation is attempted.")

    def pause(seconds):
        remaining()
        sleep(min(seconds, max(0., http.deadline - monotonic())))
        remaining()

    def matches(runner, status, *, plant=False, allow_prepared=False):
        run_key, start_key = ("run_id", "experiment_started_at") if plant else ("current_run_id", "started_at")
        expected = runner.started_at
        if (status.get(run_key) != runner.run_id
                or not (same_start(status.get(start_key), expected)
                        or allow_prepared and expected is None and status.get(start_key) is None)):
            raise RunnerError("Experiment identity changed; no foreign run may be stopped or initialized.")

    def make_runner(name):
        remaining(10)
        runner = runner_factory(replace(config, output=config.output / name), client=http,
            monotonic=monotonic, sleep=pause, now=now)
        runners.append(runner)
        runner.preflight()
        runner.prepare()
        return runner

    def wait_ticks(runner, count):
        ticks = set()
        while True:
            remaining(15)
            try:
                runner.observe_once()
            except HttpReadTimeout:
                pause(.2)
                continue
            status = runner.latest_status or {}
            matches(runner, status)
            process, output = (status.get("integrations") or {}).get("process") or {}, status.get("last_control_output") or {}
            if (status.get("status") == "running" and status.get("active") is True
                    and process.get("sample_valid") is True and process.get("missing_fields") == []
                    and finite(process.get("sample_age_s")) and 0 <= process["sample_age_s"] <= 5
                    and output.get("run_id") == runner.run_id and type(output.get("tick_seq")) is int
                    and (output.get("result") or {}).get("valid") is True
                    and output.get("process_write_error") is None
                    and verified_write({"command": process.get("last_command"), "receipt": process.get("last_write_result"),
                                        "write": output.get("process_write")}, run_id=runner.run_id, started_at=runner.started_at)):
                ticks.add(output["tick_seq"])
                if len(ticks) >= count:
                    return capture("verified_ticks", {"identity": runner.identity(), "ticks": sorted(ticks), "controller": status})
            pause(min(config.poll_s, 1))

    def end_owned(runner):
        if runner.run_id is None:
            return None
        if runner.run_id not in end_attempted:
            system = runner.call("central", "GET", "/api/system/status")
            current = system.get("current_experiment") or {}
            if current.get("run_id") != runner.run_id:
                raise RunnerError("Central selection changed; refusing to send End to another run.")
            if runner.started_at and not same_start(current.get("started_at"), runner.started_at):
                raise RunnerError("Central start identity changed; refusing End.")
            end_attempted.add(runner.run_id)
            capture("end_intent", {"identity": runner.identity(), "attempt": 1})
            response = runner.call("central", "POST", f"/api/experiments/{runner.run_id}/finish", {"expected_run_id": runner.run_id})
            capture("end_response", response)
        while True:
            remaining()
            status = runner.call("controller", "GET", "/api/status")
            experiment = runner.call("central", "GET", f"/api/experiments/{runner.run_id}")
            if runner.started_at:
                matches(runner, status)
                ready = status.get("status") == "stopped" and status.get("active") is False and bool(status.get("stopped_at"))
            else:
                ready = status.get("active") is False
            if ready and experiment.get("status") in {"completed", "error"}:
                ended[runner.run_id] = capture("end_confirmed", {"identity": runner.identity(), "controller": status, "experiment": experiment})
                break
            pause(.25)
        plant = runner.call("plant", "GET", "/status")
        if plant.get("run_id") is not None:
            matches(runner, plant, plant=True, allow_prepared=True)
            if plant.get("status") in {"running", "prepared"}:
                if runner is old and stop_attempted or runner.run_id in normal_plant_stop_attempted:
                    raise RunnerError("Prior plant-stop outcome remains unknown; the mutation will not be repeated.")
                normal_plant_stop_attempted.add(runner.run_id)
                capture("normal_plant_stop_intent", {"identity": runner.identity(), "attempt": 1})
                plant = runner.call("plant", "POST", "/stop", runner.identity())
            if plant.get("running") is not False or plant.get("status") not in {"stopped", "error"}:
                raise RunnerError("Owned plant is not confirmed inactive after End.")
        capture("plant_inactive", plant)
        return ended[runner.run_id]

    def archive_report(runner):
        if runner.run_id in reports:
            return
        if runner.started_at and runner.run_id in ended:
            runner._validate_completed_archive()
            capture("archive_validation", {"identity": runner.identity(), "validation": runner.archive_validation})
            if runner.archive_validation.get("status") != "PASS":
                raise RunnerError("Owned model-stop/recovery archive did not pass.")
            audit = check_command_archive(runner.config.output, runner.run_id, runner.started_at)
            capture("archive_command_audit", {"identity": runner.identity(), "audit": audit})
        runner.write_report()
        reports.add(runner.run_id)
        runner.log.close()

    try:
        old = make_runner("fault-run")
        wait_ticks(old, 2)
        plant = old.call("plant", "GET", "/status")
        matches(old, plant, plant=True)
        if plant.get("status") != "running" or plant.get("running") is not True or plant.get("error"):
            raise RunnerError("Fault injection needs this owned healthy running model.")
        observed_ticks = set(old.observed_ticks)
        stop_attempted = True
        intent = capture("model_stop_intent", {"identity": old.identity(), "attempt": 1})
        response = old.call("plant", "POST", "/stop", old.identity())
        stopped = capture("model_stop_response", response)
        matches(old, response, plant=True)
        if response.get("status") != "stopped" or response.get("running") is not False:
            raise RunnerError("The single stop request did not confirm an inactive owned model.")
        try:
            old.observe_once()
        except RunnerError as exc:
            if str(exc) != "Test plant stopped or reported an error.":
                raise RunnerError(f"Runner rejected input for an unrelated reason: {exc}") from exc
            latest = old.latest_plant or {}
            matches(old, latest, plant=True)
            if latest.get("running") is not False or latest.get("status") != "stopped" or old.observed_ticks != observed_ticks:
                raise ModelStopSafetyFailure("Runner accepted/reset samples instead of rejecting the stopped model.")
            capture("supervisor_rejected_model", {"reason": str(exc), "plant": latest, "controller": old.latest_status,
                "accepted_ticks_before": sorted(observed_ticks), "accepted_ticks_after": sorted(old.observed_ticks)})
        else:
            raise ModelStopSafetyFailure("Runner accepted a stopped model as normal physical evolution.")
        # End immediately after rejection. Independent fresh-but-frozen reads
        # are collected only after Controller confirms stopped.
        confirmed = end_owned(old)
        checks["supervisor_rejected_and_ended_owned_run"] = "PASS"
        startup_readiness = deepcopy(old.startup_telegraf_readiness)
        try:
            old._probe_fresh_telegraf(stopped["observed_at"])
            proof = deepcopy(old.startup_telegraf_readiness)
        finally:
            old.startup_telegraf_readiness = startup_readiness
        frozen = verify_frozen_rounds(proof, old.server_identity["node_ids"], stopped["observed_at"])
        capture("fresh_reads_of_frozen_physics", {"boundary_kind": "model_stop", "proof": proof, "validation": frozen})
        checks["fresh_telegraf_does_not_mean_fresh_physics"] = "PASS"
        after_plant = old.call("plant", "GET", "/status")
        matches(old, after_plant, plant=True)
        if any(after_plant.get(key) != response.get(key) for key in ("heartbeat_at", "elapsed_s", "T_K", "T_j_K", "c", "sample_seq")):
            raise ModelStopSafetyFailure("Stopped model advanced without a new owned run.")
        noresume_until = monotonic() + 31
        while monotonic() < noresume_until:
            remaining(45)
            status = old.call("controller", "GET", "/api/status")
            matches(old, status)
            original = confirmed["body"]["controller"]
            if (status.get("status") != "stopped" or status.get("active") is not False
                    or status.get("last_control_output") != original.get("last_control_output")
                    or status.get("scheduler", {}).get("tick_count") != original.get("scheduler", {}).get("tick_count")):
                raise ModelStopSafetyFailure("Old Controller resumed or advanced after confirmed End.")
            capture("no_old_resume", status)
            pause(1)
        checks["no_old_resume_two_periods"] = "PASS"
        capture("inflight_boundary", {"stop_intent_at": intent["observed_at"], "model_stop_confirmed_at": stopped["observed_at"],
            "controller_end_confirmed_at": confirmed["observed_at"],
            "last_control_output": confirmed["body"]["controller"].get("last_control_output"),
            "scope": "A legitimate in-flight cycle may finish between model stop and Controller End; no fabricated invalid tick or zero-output claim."})
        archive_report(old)
        remaining(35)
        fixture = recovery_fixture(old, config.output / "recovery-fixture.json")
        capture("recovery_fixture", fixture)
        if fixture.get("action_status") != "PASS":
            raise RunnerError("Guarded inactive recovery initialization failed.")
        recovery = make_runner("recovery-run")
        if recovery.run_id == old.run_id or epoch(recovery.started_at) <= epoch(old.started_at):
            raise ModelStopSafetyFailure("Recovery must have a distinct later experiment identity.")
        wait_ticks(recovery, 1)
        checks["distinct_new_run_recovered"] = "PASS"
        end_owned(recovery)
        archive_report(recovery)
        checks["owned_archives_and_commands"] = "PASS"
    except (Exception, KeyboardInterrupt) as exc:
        fail(exc, "capture")
    finally:
        # Cleanup has the reserved tail of the same 180 s envelope. End/stop
        # intents are remembered, so an uncertain mutation is never resent.
        http.deadline = final_deadline
        for runner in reversed(runners):
            if runner.log._stream.closed:
                continue
            try:
                end_owned(runner)
                archive_report(runner)
            except (Exception, KeyboardInterrupt) as exc:
                fail(exc, "owned_cleanup")
                try:
                    if runner.run_id not in reports:
                        runner.write_report()
                        reports.add(runner.run_id)
                except Exception as report_error:
                    fail(report_error, "ordinary_report")
                finally:
                    runner.log.close()
        try:
            after_sources = source_provider()
        except Exception as exc:
            after_sources = {"status": "UNAVAILABLE", "error": str(exc)}
            fail(exc, "source_fingerprint")
        elapsed = monotonic() - began
        if after_sources.get("source_sha256") != source_before.get("source_sha256"):
            errors.append({"phase": "source", "message": "Source/configuration changed during the model-stop test."})
        harness_after = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if harness_after != harness_before:
            errors.append({"phase": "host_harness", "message": "Host model-stop collector changed during capture."})
        if elapsed > total_timeout_s:
            errors.append({"phase": "budget", "message": "The complete model-stop test exceeded its declared real-time budget."})
        evidence = {"case": "supervised_model_stop", "scope": "One owned model stop, actual runner rejection, normal End and distinct recovery; not hardware/Prosys outage/paper acceptance.",
            "budget_s": total_timeout_s, "elapsed_s": elapsed, "captures": captures, "errors": errors, "checks": checks,
            "source_manifest": source_before, "source_manifest_at_end": after_sources,
            "host_harness_sha256": harness_before, "host_harness_sha256_at_end": harness_after,
            "fault_stop_attempted": stop_attempted, "end_attempted_run_ids": sorted(end_attempted),
            "ordinary_plant_stop_attempted_run_ids": sorted(normal_plant_stop_attempted), "completed_at": now()}
        required = {"supervisor_rejected_and_ended_owned_run", "fresh_telegraf_does_not_mean_fresh_physics",
            "no_old_resume_two_periods", "distinct_new_run_recovered", "owned_archives_and_commands"}
        result = {"case": evidence["case"], "status": "FAIL" if any(error.get("unsafe_observation") for error in errors)
            else "PASS" if not errors and required <= checks.keys() else "INCOMPLETE",
            "scope": evidence["scope"], "checks": checks, "errors": errors, "elapsed_s": elapsed,
            "budget_s": total_timeout_s, "fault_evidence": "fault_evidence.json"}
        save_exclusive(config.output / "fault_evidence.json", evidence)
        save_exclusive(config.output / "fault_result.json", result)
        log.close()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-stop", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    args = parser.parse_args(argv)
    if not args.execute_stop:
        parser.error("--execute-stop is required before any model/experiment action")
    result = run_model_stop_check(RunnerConfig(output=args.output, mode="short-dynamic",
        commissioning_evidence=args.commissioning_evidence, telemetry_config=args.telemetry_config), execute_stop=True)
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
