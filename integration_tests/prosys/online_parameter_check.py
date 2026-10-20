"""Explicit host-only, no-seed Controller parameter transaction check on Prosys.

This is not a paper experiment, GSensor-image check or fault acceptance. Every
mutation is a single ordinary Central/owned-plant request. Deliberate replay is
only performed after the original transaction has been positively confirmed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crystallization_mpc.testing.prosys.runner import (
    EvidenceLog, ProsysRunner, RunnerConfig, RunnerError, epoch, same_start,
    source_manifest, utc_now,
)
from crystallization_mpc.testing.prosys.growth_fault_check import _prepare_bounded_recovery


RUN_BUDGET_S = 600.0
CLEANUP_RESERVE_S = 120.0
BASELINE = {"shared": {"dt": 15.0}, "controller": {
    "params.E_A": 150000.0, "params.n": 1.75, "params.dT": .01}}
SCOPE = ("Independent no-seed, adaptation-disabled Controller-only live parameter fixture. "
         "Synthetic G remains disabled; real GSensor/image parameters and paper four stages are NOT_RUN.")


def raw_draft(document):
    """Keep every saved field, including non-catalog fixture keys such as k_0."""
    if (not isinstance(document, dict) or type(document.get("version")) is not int
            or document["version"] <= 0 or any(not isinstance(document.get(section), dict)
                                               for section in ("shared", "gsensor", "controller"))):
        raise RunnerError("A complete authoritative raw startup draft/version is required.")
    return {key: deepcopy(document[key]) for key in ("version", "shared", "gsensor", "controller")}


def close(a, b):
    return isinstance(a, (int, float)) and not isinstance(a, bool) and math.isfinite(a) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-10)


def ensure_values(status, expected):
    actual = (status.get("run_parameters") or {}).get("values") or {}
    if any(not close(actual.get(section, {}).get(key), value)
           for section, fields in expected.items() for key, value in fields.items()):
        raise RunnerError("Confirmed live Controller parameter values differ from the requested values.")


def revision(status):
    value = (status.get("runtime_controls") or {}).get("revision")
    if type(value) is not int or value < 0:
        raise RunnerError("Controller has no valid runtime revision.")
    return value


def refresh_count(status):
    value = ((status.get("runtime_controls") or {}).get("refresh") or {}).get("count")
    if type(value) is not int or value < 0:
        raise RunnerError("Controller has no valid committed refresh count.")
    return value


def verify_refresh(output):
    diagnostics = output.get("diagnostics") or {}
    fields, raw = diagnostics.get("fields") or {}, diagnostics.get("raw") or {}
    filters = raw.get("filters") or {}
    result = output.get("result") or {}
    wanted = [result.get("T"), 0., result.get("c"), 0.]
    state = (filters.get("main") or {}).get("state")
    checks = {
        "tick_aligned": diagnostics.get("tick_seq") == output.get("tick_seq")
            and diagnostics.get("run_id") == output.get("run_id"),
        "refresh_applied": fields.get("diag_refresh_applied") is True,
        "state_initialized": isinstance(state, list) and len(state) == 4
            and all(close(a, b) for a, b in zip(state, wanted)),
        "five_covariances_initialized": set(filters) == {"main", "G", "E_A", "k_0", "n"}
            and all(isinstance(item.get("covariance"), list) and bool(item["covariance"])
                    and item["covariance"] == item.get("initial_covariance") for item in filters.values()),
        "integrals_zero": all(fields.get(key) == 0 for key in ("diag_int_target", "diag_int_dT_dt", "diag_int_T")),
        "new_segment": fields.get("diag_sample_count") == 0
            and fields.get("diag_segment_start_tick") == output.get("tick_seq"),
    }
    if not all(checks.values()):
        raise RunnerError("Parameter refresh has absent or contradictory initialization diagnostics.")
    return checks


class ParameterFixtureRunner(ProsysRunner):
    """Reuse real startup/transport/archive paths, without claiming paper/fault PASS."""
    def __init__(self, *args, **kwargs):
        self._prestart_raw_observation = None
        self.raw_startup_draft = None
        super().__init__(*args, **kwargs)

    def call(self, service, method, path, payload=None):
        if service == "central" and method == "POST" and path == "/api/operation/experiment/start":
            observed = self._prestart_raw_observation
            if not observed or self.started_at is not None:
                raise RunnerError("No authoritative pre-start raw parameter response; refusing experiment start.")
            frozen = raw_draft(observed["response"])
            if frozen != raw_draft(payload):
                raise RunnerError("Actual experiment start request differs from the frozen raw parameter response.")
            self.raw_startup_draft = deepcopy(frozen)
            self.log.append("online_parameter_raw_startup_draft_before_start", {
                "observed_at": observed["observed_at"], "run_id": self.run_id,
                "raw_response": deepcopy(observed["response"]), "frozen_draft": frozen,
                "start_request": deepcopy(payload)})
        response = super().call(service, method, path, payload)
        if (service == "central" and method == "GET" and path == "/api/params"
                and self.run_id is not None and self.started_at is None):
            # base.prepare performs this GET after saving its startup draft,
            # immediately before the real start POST. Do not use a later live
            # overlay, whose intentionally closed 91-field catalog may differ.
            raw_draft(response)
            self._prestart_raw_observation = {"observed_at": self.now(), "response": deepcopy(response)}
        return response

    def write_report(self):
        ending = source_manifest()
        report = {"schema": "prosys_online_parameter_run_v1", "scope": SCOPE,
            "identity": self.identity(), "completed_at": self.now(),
            "server_identity": self.server_identity, "source_manifest": self.initial_sources,
            "source_manifest_at_end": ending, "archive": self.archive,
            "raw_startup_draft": deepcopy(self.raw_startup_draft),
            "archive_validation": self.archive_validation, "shutdown_errors": self.shutdown_errors,
            "evidence_jsonl": str(self.log.path), "verified_writes": self.writes_verified,
            "paper_procedure": "NOT_RUN", "fault_acceptance": "NOT_RUN", "real_gsensor": "NOT_RUN"}
        with self.report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
        return report


def run_check(config, *, execute=False, runner_factory=ParameterFixtureRunner,
              monotonic=time.monotonic, sleep=time.sleep, now=utc_now, manifest=source_manifest,
              recovery_fixture=_prepare_bounded_recovery):
    if execute is not True:
        raise ValueError("--execute-parameter-test is required before any experiment mutation.")
    if config.mode != "fault-fixture" or config.fault_fixture_scope != "static-readonly" or config.seed != 123:
        raise ValueError("Online parameter fixture requires seed 123 and the no-G archive scope.")
    log = EvidenceLog(config.output, now)
    original_manifest = manifest()
    checks, runs, transactions, errors, snapshots = {}, [], [], [], []
    runner = None
    cleanup_attempted = False
    deadline = active_deadline = None
    startup_draft = None
    after_end_draft = None

    def capture(kind, body):
        item = {"observed_at": now(), "body": deepcopy(body)}
        log.append(kind, item)
        return item

    def budget(cleanup=False):
        if monotonic() >= (deadline if cleanup else active_deadline):
            raise TimeoutError("Bounded online parameter fixture deadline reached; no mutation is retried.")

    def read():
        budget()
        runner.observe_once()
        status, plant = runner.latest_status, runner.latest_plant
        if (status.get("current_run_id") != runner.run_id or not same_start(status.get("started_at"), runner.started_at)
                or status.get("status") != "running" or status.get("active") is not True):
            raise RunnerError("Controller identity/state changed during the owned parameter fixture.")
        controls = (status.get("runtime_controls") or {}).get("configuration") or {}
        if (controls.get("control_target") != "sigma" or controls.get("adaptation_enabled") is not False
                or (status.get("seed_events") or {}).get("count") != 0 or plant.get("seed_event_ids") != []):
            raise RunnerError("Fixture must remain sigma-controlled, unseeded and adaptation-disabled.")
        if (status.get("measurement_parameter_revision", 0) != 0
                or not (status.get("run_parameters") or {}).get("supported")):
            raise RunnerError("Unexpected measurement revision or unavailable online parameter protocol.")
        capture("online_parameter_snapshot", {"controller": status, "plant": plant})
        output = status.get("last_control_output") or {}
        if output.get("tick_seq"):
            if not snapshots or snapshots[-1]["tick_seq"] != output["tick_seq"]:
                if snapshots:
                    previous = snapshots[-1]
                    if output["tick_seq"] != previous["tick_seq"] + 1:
                        raise RunnerError("A committed Controller tick was missed; timing proof is incomplete.")
                    if not close(output.get("elapsed_s", 0) - previous.get("elapsed_s", 0), output.get("controller_dt_s")):
                        raise RunnerError("Elapsed time reset or changed discontinuously across parameter application.")
                snapshots.append(deepcopy(output))
        return status

    def wait(predicate, label, seconds=50):
        until = min(monotonic() + seconds, active_deadline)
        while monotonic() < until:
            status = read()
            if predicate(status):
                return status
            sleep(min(1., config.poll_s))
        raise RunnerError(f"No confirmed {label}; never resend an uncertain request.")

    def draft_view(document):
        return raw_draft(document)

    def submit(changes, label, *, replay=None, no_change=False):
        before = read()
        tick_before = (before.get("last_control_output") or {}).get("tick_seq", 0)
        expected_revision, count = revision(before), refresh_count(before)
        request = deepcopy(replay) if replay else {"expected_run_id": runner.run_id,
            "experiment_started_at": runner.started_at, "version": startup_draft["version"],
            "event_id": uuid4().hex, "expected_revision": expected_revision,
            "requested_at": now(), "changes": deepcopy(changes)}
        budget()
        response = runner.call("central", "POST", "/api/params", request)
        transaction = {"label": label, "request": request, "response": response,
            "before": deepcopy(before), "deliberate_confirmed_replay": replay is not None}
        transactions.append(transaction)
        capture("online_parameter_request", transaction)
        accepted = read()
        accepted_tick = (accepted.get("scheduler") or {}).get("tick_count",
            (accepted.get("last_control_output") or {}).get("tick_seq", 0))
        transaction["acceptance_observation"] = deepcopy(accepted)
        def applied(status):
            result = (status.get("run_parameters") or {}).get("last_result") or {}
            if result.get("event_id") != request["event_id"]:
                return False
            if result.get("accepted") is False or result.get("status") == "rejected":
                raise RunnerError("Controller rejected an expected-valid parameter transaction.")
            return result.get("status") == "applied"
        status = wait(applied, label)
        result = status["run_parameters"]["last_result"]
        ensure_values(status, changes)
        if replay or no_change:
            if revision(status) != expected_revision or refresh_count(status) != count:
                raise RunnerError("Replay/no-change request caused a second revision or refresh.")
            if no_change and result.get("no_change") is not True:
                raise RunnerError("Equal submitted values were not acknowledged as no-change.")
        else:
            effective = result.get("effective_tick")
            if (revision(status) != expected_revision + 1 or refresh_count(status) != count + 1
                    or result.get("refreshed") is not True or type(effective) is not int
                    or not tick_before < effective <= accepted_tick + 1):
                raise RunnerError("Parameter transaction has no single committed next-cycle revision/refresh.")
            committed = next((item for item in snapshots if item["tick_seq"] == effective), None)
            if committed is None:
                raise RunnerError("Refresh initialization cycle was not captured; do not infer it from a later cycle.")
            transaction["refresh_checks"] = verify_refresh(committed)
            following = wait(lambda value: (value.get("last_control_output") or {}).get("tick_seq", 0) >= effective + 1,
                             label + " next-cycle recovery")
            output = next(item for item in snapshots if item["tick_seq"] == effective + 1)
            expected_dt = following["run_parameters"]["values"]["shared"]["dt"]
            gap = epoch(output["computed_at"]) - epoch(committed["computed_at"])
            if (not close(output.get("controller_dt_s"), expected_dt)
                    or not close(output["elapsed_s"] - committed["elapsed_s"], expected_dt)
                    or not expected_dt - .5 <= gap <= expected_dt + 5
                    or (output.get("diagnostics") or {}).get("fields", {}).get("diag_main_lifecycle") != "updated"):
                raise RunnerError("Next committed cycle did not resume at the new period with continuous elapsed time.")
            transaction.update(effective_tick=effective, next_tick=effective + 1,
                               next_interval_real_s=gap, next_interval_nominal_s=expected_dt)
        transaction["after"] = deepcopy(status)
        capture("online_parameter_confirmed", transaction)
        return request

    def cleanup():
        """One End, prove inactive, then one owned-model stop; never blanket stop."""
        nonlocal cleanup_attempted, after_end_draft
        if runner is None:
            return
        if cleanup_attempted:
            raise RunnerError("Owned cleanup was already attempted; never repeat an uncertain End/stop.")
        cleanup_attempted = True
        runner.operation_deadline = deadline
        try:
            if runner.run_id:
                budget(cleanup=True)
                system = runner.call("central", "GET", "/api/system/status")
                central_run = system.get("current_experiment") or {}
                controller_before = runner.call("controller", "GET", "/api/status")
                previous = runner.preflight_controller_identity
                if (central_run.get("run_id") != runner.run_id or (runner.started_at
                        and not same_start(central_run.get("started_at"), runner.started_at))
                        or (not runner.started_at and central_run.get("started_at") is not None)):
                    raise RunnerError("Central ownership changed; cleanup will not finish another run.")
                controller_owned = (controller_before.get("current_run_id") == runner.run_id
                    and (same_start(controller_before.get("started_at"), runner.started_at)
                         if runner.started_at else controller_before.get("started_at") is None))
                original_inactive = (not runner.started_at and previous is not None
                    and (controller_before.get("current_run_id"), controller_before.get("started_at"),
                         controller_before.get("status")) == previous and controller_before.get("active") is False)
                if not (controller_owned or original_inactive):
                    raise RunnerError("Controller run/start ownership changed; no End or model stop is permitted.")
                runner.call("central", "POST", f"/api/experiments/{runner.run_id}/finish",
                            {"expected_run_id": runner.run_id})
                until = min(monotonic() + 45, deadline)
                while monotonic() < until:
                    state = runner.call("controller", "GET", "/api/status")
                    experiment = runner.call("central", "GET", f"/api/experiments/{runner.run_id}")
                    same_owned = state.get("current_run_id") == runner.run_id and (
                        not runner.started_at or same_start(state.get("started_at"), runner.started_at))
                    previous = runner.preflight_controller_identity
                    never_started = (not runner.started_at and previous is not None and
                        (state.get("current_run_id"), state.get("started_at"), state.get("status")) == previous)
                    owned_stopped = (same_owned and state.get("status") == "stopped" and bool(state.get("stopped_at")))
                    if (owned_stopped or never_started) and state.get("active") is False and experiment.get("status") in {"completed", "error"}:
                        capture("online_parameter_end_confirmed", {"controller": state, "experiment": experiment})
                        break
                    sleep(1)
                else:
                    raise RunnerError("Owned Controller stop was not confirmed; do not stop its model.")
                plant = runner.call("plant", "GET", "/status")
                if (plant.get("run_id") != runner.run_id or (runner.started_at
                        and not same_start(plant.get("experiment_started_at"), runner.started_at))):
                    raise RunnerError("Plant ownership changed; will not stop another model.")
                runner.call("plant", "POST", "/stop", runner.identity())
                stopped = runner.call("plant", "GET", "/status")
                if (stopped.get("run_id") != runner.run_id or stopped.get("running") is not False
                        or stopped.get("status") != "stopped"
                        or (runner.started_at and not same_start(stopped.get("experiment_started_at"), runner.started_at))):
                    raise RunnerError("Owned plant stop was not confirmed.")
                capture("online_parameter_plant_stopped", stopped)
                if runner.started_at:
                    runner._validate_completed_archive()  # actual five CSVs, require_g=False by explicit config scope
                    if runner.archive_validation.get("status") != "PASS":
                        raise RunnerError("Owned no-G CSV archive did not validate completely.")
                after_end_draft = draft_view(runner.call("central", "GET", "/api/params"))
                capture("online_parameter_startup_draft_after_end", after_end_draft)
        except Exception as exc:
            runner.shutdown_errors.append(str(exc))
            raise
        finally:
            try:
                report = runner.write_report()
                runs.append(report)
            finally:
                runner.log.close()

    def start(label):
        nonlocal runner, deadline, active_deadline, snapshots, cleanup_attempted
        cleanup_attempted = False
        deadline = monotonic() + RUN_BUDGET_S
        active_deadline = deadline - CLEANUP_RESERVE_S
        snapshots = []
        runner = runner_factory(replace(config, output=config.output / label, max_hours=RUN_BUDGET_S / 3600),
            monotonic=monotonic, sleep=sleep, now=now)
        runner.operation_deadline = active_deadline
        runner.preflight()
        runner.prepare()
        if not isinstance(getattr(runner, "raw_startup_draft", None), dict):
            raise RunnerError("Pre-start raw startup evidence is missing; never substitute an active overlay.")
        status = wait(lambda value: bool((value.get("last_control_output") or {}).get("tick_seq")), "initial output")
        ensure_values(status, BASELINE)
        return status

    try:
        capture("online_parameter_started", {"scope": SCOPE, "source_manifest": original_manifest,
            "per_run_maximum_s": RUN_BUDGET_S, "cleanup_reserve_s": CLEANUP_RESERVE_S,
            "timing_tolerance_real_s": {"early": .5, "late": 5}})
        start("parameter-run")
        startup_draft = raw_draft(runner.raw_startup_draft)
        request = submit({"controller": {"params.E_A": 145000., "params.n": 1.70}}, "batch_E_A_n")
        checks["batch_E_A_n"] = True
        submit(request["changes"], "confirmed_exact_event_replay", replay=request)
        checks["confirmed_event_replay_no_duplicate"] = True
        submit(request["changes"], "new_event_same_values", no_change=True)
        checks["no_change_no_revision"] = True
        before = read()
        invalid = {"expected_run_id": runner.run_id, "experiment_started_at": runner.started_at,
            "version": startup_draft["version"], "event_id": uuid4().hex,
            "expected_revision": revision(before), "requested_at": now(), "changes": {"shared": {"dt": -1}}}
        try:
            budget()
            runner.call("central", "POST", "/api/params", invalid)
        except RunnerError as exc:
            if not isinstance(exc.__cause__, HTTPError) or exc.__cause__.code not in {400, 422}:
                raise  # unknown mutation outcome is never retried or called a rejection PASS
            capture("online_parameter_invalid_known_rejection", {"request": invalid, "error": str(exc)})
        else:
            raise RunnerError("Invalid negative period was unexpectedly accepted.")
        after = read()
        if revision(after) != revision(before) or refresh_count(after) != refresh_count(before):
            raise RunnerError("Rejected invalid request altered the committed revision/refresh.")
        checks["invalid_value_rejected_without_apply"] = True
        submit({"controller": {"params.dT": .011}}, "temperature_measurement_noise")
        checks["filter_parameter_refresh"] = True
        submit({"shared": {"dt": 10.}}, "controller_period")
        checks["period_and_elapsed_continuity"] = True
        submit(BASELINE, "atomic_restore_original_values")
        checks["restore_original_values"] = True
        cleanup()
        if after_end_draft != startup_draft:
            raise RunnerError("Runtime transactions changed the saved startup draft/version.")
        checks["startup_draft_unchanged"] = True
        # The prior run may leave a cold jacket setpoint. Use the existing
        # InactiveGuard+Commissioning route and independent readback, not model
        # reset or a silent assumption that Prosys still holds the initial 42 C.
        def recovery_remaining():
            budget(cleanup=True)
            return deadline - monotonic()
        prepared = recovery_fixture(runner, config.output / "startup-dynamic-initialization.json", recovery_remaining)
        capture("online_parameter_next_run_dynamic_initialization", prepared)
        if prepared.get("action_status") != "PASS":
            raise RunnerError("Guarded actual Prosys dynamic initial setpoint was not verified.")
        checks["new_run_dynamic_initialization"] = True
        runner = None
        # Independent second short run proves actual startup, not merely draft display.
        start("startup-baseline-run")
        if raw_draft(runner.raw_startup_draft) != startup_draft:
            raise RunnerError("Independent new experiment did not start from the same complete saved raw draft/version.")
        ensure_values(read(), BASELINE)
        checks["new_run_actual_startup_baseline"] = True
        cleanup()
        runner = None
    except BaseException as exc:
        errors.append({"type": type(exc).__name__, "message": str(exc), "observed_at": now()})
        capture("online_parameter_failure", errors[-1])
        if runner is not None and not cleanup_attempted:
            try:
                cleanup()
            except BaseException as ending_error:
                errors.append({"type": type(ending_error).__name__, "message": str(ending_error), "phase": "owned_cleanup"})
    finally:
        final = manifest()
        checks["source_unchanged"] = final == original_manifest
        all_checks = {name: "PASS" if value else "FAIL" for name, value in checks.items()}
        required = {"batch_E_A_n", "confirmed_event_replay_no_duplicate", "no_change_no_revision",
            "invalid_value_rejected_without_apply", "filter_parameter_refresh", "period_and_elapsed_continuity",
            "restore_original_values", "startup_draft_unchanged", "new_run_actual_startup_baseline", "source_unchanged"}
        required.add("new_run_dynamic_initialization")
        for name in required - set(all_checks):
            all_checks[name] = "NOT_RUN"
        report = {"schema": "prosys_online_parameter_check_v1", "scope": SCOPE,
            "status": "PASS" if not errors and all(value == "PASS" for value in all_checks.values()) else "FAIL",
            "checks": all_checks, "transactions": transactions, "runs": runs, "errors": errors,
            "source_manifest": original_manifest, "source_manifest_at_end": final,
            "evidence_jsonl": str(log.path), "completed_at": now(),
            "limitations": ["No real GSensor/camera/hardware validation.", "No paper-stage or control-performance verdict."]}
        with (config.output / "parameter_result.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
        log.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-parameter-test", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    args = parser.parse_args(argv)
    if not args.execute_parameter_test:
        parser.error("Explicit --execute-parameter-test is required.")
    config = RunnerConfig(output=args.output, seed=123, mode="fault-fixture", fault_fixture_scope="static-readonly",
        max_hours=RUN_BUDGET_S / 3600, commissioning_evidence=args.commissioning_evidence,
        telemetry_config=args.telemetry_config, archive_timeout_s=60)
    report = run_check(config, execute=True)
    print(json.dumps({"status": report["status"], "checks": report["checks"], "report": str(args.output / "parameter_result.json")}))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
