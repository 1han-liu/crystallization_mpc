"""Opt-in isolated short run: real data-link evidence or human-operated UI.

Never submits runtime parameter changes, seeds, activation or paper-stage
actions. The UI mode only observes changes made through the actual browser.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crystallization_mpc.testing.prosys.runner import HttpReadTimeout, ProsysRunner, RunnerConfig, RunnerError, epoch, same_start, source_manifest
from integration_tests.prosys.online_parameter_check import ParameterFixtureRunner, raw_draft
from integration_tests.prosys.managed_paper_run import SignalLatch, installed_signal_handlers


STARTUP_S = 180.
CLEANUP_S = 180.
FIELDS = ("T", "T_j", "c", "count_middle", "T_j_set")
GOOD = {"0", "0x0", "OK (0x0)", "The operation succeeded. StatusGood (0x0)"}


class BoundedFixtureRunner(ParameterFixtureRunner):
    """The inherited startup freezes RAW before Start; all HTTP shares a deadline."""

    def _bounded_io(self, operation, *args):
        deadline = self.operation_deadline
        if deadline is None:
            return super()._bounded_io(operation, *args)
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("Interactive short operation deadline reached; never retry an uncertain mutation.")
        previous = self.client.timeout_s
        self.client.timeout_s = min(previous, remaining)
        try:
            try:
                value = operation(*args)
            except HttpReadTimeout as exc:
                if self.monotonic() >= deadline:
                    raise TimeoutError("Shared interactive deadline expired during HTTP read.") from exc
                raise  # An early read failure is a real failure, not normal expiry.
            if self.monotonic() >= deadline:
                raise TimeoutError("Shared interactive deadline expired during I/O; late results are not evidence.")
            return value
        finally:
            self.client.timeout_s = previous

    def write_report(self):
        if self.config.mode == "short-dynamic":
            return ProsysRunner.write_report(self)
        return super().write_report()


def same_identity(document, run_id, started_at, *, controller=False, plant=False):
    actual = document.get("current_run_id" if controller else "run_id")
    start = document.get("experiment_started_at" if plant else "started_at")
    return actual == run_id and (same_start(start, started_at) if started_at is not None else start is None)


def valid_process_tick(status, now):
    process = ((status.get("integrations") or {}).get("process") or {})
    output = status.get("last_control_output") or {}
    sample = process.get("last_state") or output.get("process_state") or {}
    try:
        age = epoch(now) - epoch(sample["read_at"])
    except (KeyError, ValueError, TypeError):
        return False
    return (process.get("sample_valid") is True and not process.get("missing_fields")
            and not process.get("invalid_fields") and -.01 <= age <= 5
            and (output.get("result") or {}).get("valid") is True
            and all(isinstance(sample.get(key), (int, float)) and not isinstance(sample[key], bool)
                    and math.isfinite(sample[key]) and str((sample.get("status_codes") or {}).get(key)) in GOOD
                    for key in FIELDS))


def run_short(config, *, mode, execute=False, active_seconds=180., runner_factory=BoundedFixtureRunner,
              monotonic=time.monotonic, sleep=time.sleep, manifest=source_manifest, now=None, latch=None):
    if execute is not True:
        raise ValueError("Explicit --execute-short is required before any experiment mutation.")
    if (mode not in {"data-link", "ui-parameters"} or isinstance(active_seconds, bool)
            or not isinstance(active_seconds, (int, float)) or not math.isfinite(active_seconds)
            or not 0 < active_seconds <= 180):
        raise ValueError("Choose data-link or ui-parameters and positive active-seconds <= 180.")
    required_mode = "short-dynamic" if mode == "data-link" else "fault-fixture"
    config = replace(config, mode=required_mode, max_hours=active_seconds/3600., poll_s=min(config.poll_s, 1.),
                     seed=123, fault_fixture_scope="growth" if mode == "data-link" else "static-readonly")
    now = now or (lambda: datetime.now(timezone.utc).isoformat())
    original = manifest()
    runner = runner_factory(config, monotonic=monotonic, sleep=sleep, now=now)
    latch = latch or SignalLatch()
    errors, snapshots, applied, refreshes = [], [], {}, {}
    cleanup = {"status": "NOT_RUN", "end_attempts": 0, "plant_stop_attempts": 0}
    before = after = native_report = None
    consecutive = 0
    last_tick = None
    active_deadline = None
    reason = "NOT_STARTED"

    def event(kind, body):
        runner.log.append("interactive_short_" + kind, deepcopy(body))

    def record_error(stage, exc):
        item = {"stage": stage, "type": type(exc).__name__, "message": str(exc)}
        errors.append(item)
        event("error", item)

    def capture():
        nonlocal last_tick, consecutive
        runner.observe_once()
        if monotonic() >= active_deadline:
            raise TimeoutError("Active cap reached during observation; do not count a late tick.")
        status, plant = runner.latest_status, runner.latest_plant
        if (not same_identity(status, runner.run_id, runner.started_at, controller=True)
                or status.get("active") is not True or status.get("status") != "running"):
            raise RunnerError("Owned Controller run/start or active state changed; no automatic continuation.")
        if (status.get("seed_events") or {}).get("count") != 0 or plant.get("seed_event_ids") != []:
            raise RunnerError("Interactive short must remain unseeded.")
        controls = (status.get("runtime_controls") or {}).get("configuration") or {}
        if controls.get("control_target") != "sigma" or controls.get("adaptation_enabled") is not False:
            raise RunnerError("Interactive short must remain sigma-controlled with adaptation disabled.")
        if (status.get("growth_input") or {}).get("requested_enabled") is True:
            raise RunnerError("Synthetic G must remain disabled in the independent short fixture.")
        output = status.get("last_control_output") or {}
        tick = output.get("tick_seq")
        if type(tick) is not int or tick <= 0 or tick == last_tick:
            return
        if last_tick is not None and tick <= last_tick:
            raise RunnerError("Controller tick reversed; implicit restart is forbidden.")
        good = valid_process_tick(status, now())
        consecutive = consecutive+1 if good and (last_tick is None or tick == last_tick+1) else 1 if good else 0
        last_tick = tick
        snapshot = {"observed_at": now(), "controller": deepcopy(status), "plant": deepcopy(plant),
                    "five_field_tick_valid": good, "consecutive_valid_ticks": consecutive}
        snapshots.append(snapshot)
        event("snapshot", snapshot)
        parameters = status.get("run_parameters") or {}
        for result in list(parameters.get("events") or []) + [parameters.get("last_result") or {}]:
            if (result.get("status") == "applied" and result.get("run_id") == runner.run_id
                    and result.get("event_id")):
                applied[result["event_id"]] = deepcopy(result)
        diagnostic = output.get("diagnostics") or {}
        if ((diagnostic.get("fields") or {}).get("diag_refresh_applied") is True
                and diagnostic.get("tick_seq") == tick and diagnostic.get("run_id") == runner.run_id):
            refreshes[str(tick)] = deepcopy(diagnostic)

    def finish_owned():
        nonlocal after
        cleanup["status"] = "IN_PROGRESS"
        runner.operation_deadline = monotonic() + CLEANUP_S
        if runner.run_id is None:
            raise RunnerError("No confirmed owned run; uncertain creation requires manual reconciliation.")
        current = (runner.call("central", "GET", "/api/system/status").get("current_experiment") or {})
        state = runner.call("controller", "GET", "/api/status")
        if not same_identity(current, runner.run_id, runner.started_at):
            raise RunnerError("Central run/start ownership changed; no End or plant stop is permitted.")
        previous = runner.preflight_controller_identity
        never_started = (runner.started_at is None and previous is not None and state.get("active") is False
                         and (state.get("current_run_id"), state.get("started_at"), state.get("status")) == previous)
        if not (same_identity(state, runner.run_id, runner.started_at, controller=True) or never_started):
            raise RunnerError("Controller run/start ownership changed; no End or plant stop is permitted.")
        if current.get("status") in {"completed", "error"}:
            if not current.get("ended_at"):
                raise RunnerError("Terminal experiment has no ended marker; manual attention required.")
            cleanup["already_ended"] = True
        else:
            cleanup["end_attempts"] = 1
            runner.call("central", "POST", f"/api/experiments/{runner.run_id}/finish", {"expected_run_id": runner.run_id})
        stop_deadline = min(monotonic()+45., runner.operation_deadline)
        while monotonic() < stop_deadline:
            state = runner.call("controller", "GET", "/api/status")
            ended = runner.call("central", "GET", f"/api/experiments/{runner.run_id}")
            if not same_identity(ended, runner.run_id, runner.started_at):
                raise RunnerError("Ended experiment identity changed; model must not be stopped.")
            same_controller = same_identity(state, runner.run_id, runner.started_at, controller=True)
            previous_inactive = (runner.started_at is None and previous is not None
                and (state.get("current_run_id"), state.get("started_at"), state.get("status")) == previous)
            if not (same_controller or previous_inactive):
                raise RunnerError("Controller ownership changed while confirming End.")
            if (state.get("active") is False and (previous_inactive or state.get("status") == "stopped" and state.get("stopped_at"))
                    and ended.get("status") in {"completed", "error"} and ended.get("ended_at")):
                cleanup["controller_inactive"] = True
                cleanup["ended"] = deepcopy(ended)
                break
            sleep(min(.5, max(0, stop_deadline-monotonic())))
        else:
            raise RunnerError("Owned Controller did not confirm stop; do not stop its model.")
        plant = runner.call("plant", "GET", "/status")
        if not same_identity(plant, runner.run_id, runner.started_at, plant=True):
            raise RunnerError("Plant run/start ownership changed; will not stop another model.")
        if not (plant.get("running") is False and plant.get("status") == "stopped"):
            cleanup["plant_stop_attempts"] = 1
            runner.call("plant", "POST", "/stop", runner.identity())
        stopped = runner.call("plant", "GET", "/status")
        if (not same_identity(stopped, runner.run_id, runner.started_at, plant=True)
                or stopped.get("running") is not False or stopped.get("status") != "stopped"):
            raise RunnerError("Owned plant stop was not positively confirmed.")
        cleanup["plant_stopped"] = True
        if runner.started_at is not None:
            runner._validate_completed_archive()
            if runner.archive_validation.get("status") != "PASS":
                raise RunnerError("The real five-CSV archive did not validate completely.")
        after = raw_draft(runner.call("central", "GET", "/api/params"))
        if before is not None and after != before:
            raise RunnerError("Online UI edits polluted the complete saved RAW startup draft/version.")
        cleanup["status"] = "COMPLETED"
        event("cleanup", cleanup)

    try:
        runner.operation_deadline = monotonic()+STARTUP_S
        runner.preflight()
        runner.prepare()
        before = raw_draft(runner.raw_startup_draft)
        active_deadline = monotonic()+max(0., active_seconds-runner.elapsed())
        runner.operation_deadline = active_deadline
        event("ready", {"mode": mode, "identity": runner.identity(), "raw_startup_draft": before,
                        "active_maximum_s": active_seconds, "automatic_runtime_parameter_posts": False})
        print(json.dumps({"status": "READY", "mode": mode, "identity": runner.identity(),
                          "active_seconds": active_seconds, "output": str(config.output)}), flush=True)
        while monotonic() < active_deadline:
            if latch.first_signal is not None:
                raise RunnerError("Operator interrupted the short fixture; owned cleanup only.")
            capture()
            if (mode == "data-link" and consecutive >= 6
                    and all(runner.dynamic_components.get(key) == "PASS" for key in ("T", "T_j"))):
                runner.short_completed = True
                reason = "SIX_VALID_TICKS_AND_THERMAL_RESPONSE"
                break
            sleep(min(1., max(0., active_deadline-monotonic())))
        else:
            reason = "ACTIVE_TIME_LIMIT"
    except TimeoutError as exc:
        if active_deadline is not None and monotonic() >= active_deadline:
            reason = "ACTIVE_TIME_LIMIT"
        else:
            record_error("active", exc)
    except BaseException as exc:
        record_error("active", exc)
    finally:
        try:
            finish_owned()
        except BaseException as exc:
            cleanup["status"] = "MANUAL_ATTENTION"
            runner.shutdown_errors.append(str(exc))
            record_error("cleanup", exc)
        try:
            native_report = runner.write_report()
        except BaseException as exc:
            record_error("native_report", exc)
        final_sources = manifest()
        unchanged = final_sources == original
        if not unchanged:
            record_error("source", RunnerError("Frozen production source manifest changed."))
        complete = (not errors and cleanup["status"] == "COMPLETED" and before is not None and after == before and unchanged)
        data_pass = complete and mode == "data-link" and runner.short_completed and (native_report or {}).get("overall") == "PASS"
        status = ("PASS" if data_pass else "REVIEW_REQUIRED" if complete and mode == "ui-parameters"
                  else "INCOMPLETE" if complete else "FAIL")
        report = {"schema": "prosys_interactive_short_v1", "mode": mode, "status": status,
            "identity": runner.identity(), "stop_reason": reason, "active_maximum_s": active_seconds,
            "observed_ticks": len(snapshots), "consecutive_valid_ticks": consecutive,
            "thermal_response": deepcopy(runner.dynamic_components), "applied_parameter_events": list(applied.values()),
            "refresh_snapshots": refreshes, "raw_startup_before": before, "raw_startup_after": after,
            "startup_draft_unchanged": before is not None and after == before,
            "cleanup": cleanup, "errors": errors, "source_unchanged": unchanged,
            "source_manifest": original, "source_manifest_at_end": final_sources,
            "native_report": str(runner.report_path), "evidence_jsonl": str(runner.log.path),
            "limitations": ["UI screenshots/actions require separate real-browser evidence; no automatic UI PASS.",
                            "No paper four-stage or real GSensor/camera/hardware acceptance."]}
        event("result", report)
        with (config.output/"interactive_short_result.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
        runner.log.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-short", action="store_true")
    parser.add_argument("--mode", choices=("data-link", "ui-parameters"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, required=True)
    parser.add_argument("--active-seconds", type=float, default=180.)
    args = parser.parse_args(argv)
    if not args.execute_short:
        parser.error("Explicit --execute-short is required; no service action occurred.")
    config = RunnerConfig(output=args.output, commissioning_evidence=args.commissioning_evidence,
                          telemetry_config=args.telemetry_config)
    latch = SignalLatch()
    with installed_signal_handlers(latch):
        report = run_short(config, mode=args.mode, execute=True, active_seconds=args.active_seconds, latch=latch)
    print(json.dumps({"status": report["status"], "identity": report["identity"],
                      "output": str(args.output/"interactive_short_result.json")}), flush=True)
    return 0 if report["status"] in {"PASS", "REVIEW_REQUIRED"} else 4 if report["status"] == "INCOMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
