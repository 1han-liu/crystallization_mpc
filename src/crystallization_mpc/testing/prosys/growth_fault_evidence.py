"""Pure strict G-loss evidence checks, not an injector or a paper acceptance test.

The capture contract contains timestamped Controller/GSensor/container snapshots,
raw Rabbit envelopes (with original UTF-8 bytes and SHA256), and all intermediate
Controller observations. Missing proof is INCOMPLETE; a witnessed unlocked old
run is FAIL, even when a later snapshot is safely stopped. Unit fixtures are not
actual fault-test evidence. Legacy auto-resume criteria are deliberately unused.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .gsensor import SOURCE
from .runner import epoch, finite, same_start


def is_hold(status: Mapping[str, Any], reason: str) -> bool:
    recovery = status.get("recovery") or {}
    return (status.get("active") is False and status.get("status") == "recovery_required"
        and recovery.get("status") == "confirmation_required" and recovery.get("error") is None
        and recovery.get("requires_confirmation") is True and recovery.get("requires_new_run") is True
        and recovery.get("hold_reason") == reason
        and (status.get("scheduler") or {}).get("deadline_set") is False)


def fresh_growth(status: Mapping[str, Any], observed_at: str) -> bool:
    """Require a real accepted frame and a later committed verified output."""
    growth, sample, output = (status.get(key) or {} for key in ("growth_input", "last_valid_sample", "last_control_output"))
    try:
        return (status.get("active") is True and status.get("status") == "running"
            and growth.get("source") == "live_gsensor" and growth.get("available") is True
            and growth.get("requested_enabled") is True and growth.get("enabled") is True
            and growth.get("measurement_ready") is True and finite(growth.get("sample_age_s"))
            and 0 <= growth["sample_age_s"] <= 30
            and sample.get("run_id") == status.get("current_run_id")
            and same_start(sample.get("experiment_started_at"), status.get("started_at"))
            and sample.get("valid") is True and type(sample.get("frame_seq")) is int and sample["frame_seq"] > 0
            and all(sample.get(key) == growth.get(key) for key in ("control_revision", "initialization_generation", "alignment_revision"))
            and 0 <= epoch(observed_at) - epoch(sample["captured_at"]) <= 30
            and epoch(sample["captured_at"]) <= epoch(sample["processed_at"])
            and type(output.get("tick_seq")) is int and output["tick_seq"] > 0
            and output.get("run_id") == status.get("current_run_id")
            and epoch(sample["processed_at"]) <= epoch(output["computed_at"]) <= epoch(observed_at)
            and epoch(observed_at) - epoch(output["computed_at"]) <= 35
            and (output.get("result") or {}).get("valid") is True
            and output.get("growth_frame_seq") == sample["frame_seq"]
            and finite(output.get("growth_sample_age_s")) and 0 <= output["growth_sample_age_s"] <= 30
            and (output.get("process_write") or {}).get("verified") is True and output.get("process_write_error") is None)
    except (KeyError, TypeError, ValueError):
        return False


def eligible_late_frames(messages, baseline: Mapping[str, Any], resumed_at: str, through_at: str):
    """The collector and evaluator share the exact post-inspection frame gate.

    A frame processed before the successful resume inspection does not qualify,
    even when Rabbit delivers it afterwards. Invalid evidence never starts the
    collector's clock; the evaluator also validates all original wire hashes.
    """
    growth, sample = baseline.get("growth_input") or {}, baseline.get("last_valid_sample") or {}
    resumed, end = epoch(resumed_at), epoch(through_at)
    eligible = []
    for item in messages:
        try:
            envelope = item["message"]
            payload = envelope.get("payload") or {}
            observed = epoch(item["observed_at"])
            if (envelope.get("src") == "gsensor" and envelope.get("dst") == "controller"
                    and envelope.get("name") == "growth_rate.sample" and envelope.get("msg_type") == "measurement"
                    and payload.get("run_id") == baseline.get("current_run_id")
                    and same_start(payload.get("experiment_started_at"), baseline.get("started_at"))
                    and payload.get("source") == SOURCE and payload.get("synthetic") is True
                    and payload.get("valid") is True
                    and all(finite(payload.get(key)) for key in ("G_u", "G_u_KF", "G_v", "G_v_KF"))
                    and all(payload.get(key) == growth[key] for key in
                        ("control_revision", "initialization_generation", "alignment_revision"))
                    and payload.get("frame_seq", 0) > sample["frame_seq"]
                    and resumed <= observed <= end
                    and epoch(sample["captured_at"]) < epoch(payload["captured_at"])
                    and resumed <= epoch(payload["processed_at"]) <= observed
                    and 0 <= observed - epoch(payload["captured_at"]) <= 30):
                eligible.append(item)
        except (KeyError, TypeError, ValueError):
            continue
    return eligible


def evaluate_growth_fault(document: Mapping[str, Any]) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    def check(name, condition, *, failure="INCOMPLETE"):
        checks[name] = {"status": "PASS" if condition else failure}
    def capture(kind, phase):
        found = [item for item in document.get("captures", []) if item.get("kind") == kind and item.get("phase") == phase]
        if len(found) != 1:
            raise ValueError(f"Exactly one {kind}/{phase} capture is required.")
        epoch(found[0]["observed_at"])
        return found[0]
    def own(status):
        return (status.get("current_run_id") == document.get("run_id")
            and same_start(status.get("started_at"), document.get("experiment_started_at")))
    def at(item):
        return epoch(item["observed_at"])

    # Never let incomplete final evidence hide a witnessed safety violation.
    check("no_observed_safety_violation", not document.get("safety_violations"), failure="FAIL")
    check("capture_completed", not document.get("capture_errors"))
    check("source_unchanged", bool((document.get("source_manifest") or {}).get("source_sha256"))
        and (document.get("source_manifest") or {}).get("source_sha256") ==
        (document.get("source_manifest_at_end") or {}).get("source_sha256"), failure="FAIL")
    try:
        mode = document["mode"]
        if mode not in {"timeout", "restart"} or not document.get("run_id"):
            raise ValueError("Mode and original experiment identity are required.")
        reason = "growth_timeout" if mode == "timeout" else "gsensor_error"
        before, held, after, recovered = [capture("controller", phase) for phase in ("before", "held", "after", "recovered")]
        old, hold, last, new = (item["body"] for item in (before, held, after, recovered))
        check("ordered_controller_captures", at(before) < at(held) <= at(after) < at(recovered), failure="FAIL")
        check("baseline_live_G", own(old) and fresh_growth(old, before["observed_at"]))
        check("held_identity_and_contract", own(hold) and is_hold(hold, reason))
        check("old_run_stays_held", own(last) and is_hold(last, reason)
            and last.get("last_control_output") == hold.get("last_control_output")
            and (last.get("scheduler") or {}).get("tick_count") == (hold.get("scheduler") or {}).get("tick_count"), failure="FAIL")
        dt = (old.get("scheduler") or {}).get("growth_dt_s")
        control_dt = (old.get("scheduler") or {}).get("controller_dt_s")
        check("fixed_sampling_period", dt == 15 and control_dt == 15)
        check("hold_observation_duration", at(after) - at(held) >= 31)
        hold_at = epoch(hold["recovery"]["hold_occurred_at"])
        check("hold_timestamp", at(before) < hold_at <= at(held), failure="FAIL")
        check("no_output_committed_after_hold", epoch(hold["last_control_output"]["computed_at"]) <= hold_at, failure="FAIL")
        for observation in document.get("interim_observations", []):
            status = observation["body"]
            if at(observation) >= at(held):
                check("interim_" + str(len(checks)), own(status) and is_hold(status, reason)
                    and status.get("last_control_output") == hold.get("last_control_output")
                    and (status.get("scheduler") or {}).get("tick_count") == (hold.get("scheduler") or {}).get("tick_count"), failure="FAIL")
        inspections = {phase: capture("container", phase) for phase in ("before", "fault", "resumed")}
        container_id = inspections["before"]["body"]["Id"]
        def labels_match(item):
            labels = item["body"].get("Labels") or {}
            for suffix, expected in (("project", "mpcrystal-prosys"), ("service", "gsensor")):
                values = [labels.get(prefix + suffix) for prefix in ("io.podman.compose.", "com.docker.compose.") if labels.get(prefix + suffix)]
                if not values or any(value != expected for value in values):
                    return False
            return True
        check("same_isolated_container", isinstance(container_id, str) and bool(re.fullmatch(r"[0-9a-f]{64}", container_id))
            and all(item["body"].get("Id") == container_id
            and str(item["body"].get("Name", "")).lstrip("/") == "mpcrystal-prosys_gsensor_1"
            and labels_match(item)
            for item in inspections.values()), failure="FAIL")
        check("container_capture_order", at(inspections["before"]) <= at(before)
            <= at(inspections["fault"]) <= at(held) <= at(inspections["resumed"]) <= at(after), failure="FAIL")
        raw, all_samples = [], []
        for item in document.get("raw_messages", []):
            wire = item["wire_utf8"].encode("utf-8")
            if hashlib.sha256(wire).hexdigest() != item["sha256"] or json.loads(wire) != item["message"]:
                raise ValueError("Raw Rabbit message does not match its saved bytes/hash.")
            envelope, payload = item["message"], item["message"].get("payload") or {}
            if envelope.get("src") == "gsensor" and envelope.get("dst") == "controller" and envelope.get("name") == "growth_rate.sample":
                if (envelope.get("msg_type") != "measurement" or payload.get("source") != SOURCE
                        or payload.get("synthetic") is not True or payload.get("valid") is not True
                        or not all(finite(payload.get(key)) for key in ("G_u", "G_u_KF", "G_v", "G_v_KF"))):
                    raise ValueError("Raw synthetic G sample is not a complete finite measurement.")
                all_samples.append(item)
            if (envelope.get("src") == "gsensor" and envelope.get("dst") == "controller"
                    and payload.get("run_id") == document["run_id"]
                    and same_start(payload.get("experiment_started_at"), document["experiment_started_at"])):
                if payload.get("source") != SOURCE or payload.get("synthetic") is not True:
                    raise ValueError("The G source is not the explicitly labeled synthetic producer.")
                raw.append(item)
        baseline_sample = old["last_valid_sample"]
        sample_fields = ("frame_seq", "captured_at", "processed_at", "control_revision", "initialization_generation",
                         "alignment_revision", "G_u", "G_u_KF", "G_v", "G_v_KF")
        baseline_wire = [item for item in raw if item["message"].get("name") == "growth_rate.sample"
            and all(item["message"]["payload"].get(key) == baseline_sample.get(key) for key in sample_fields)
            and epoch(baseline_sample["processed_at"]) <= at(item) <= at(before)]
        check("baseline_raw_frame", len(baseline_wire) == 1)
        before_state, fault_state, resumed_state = [inspections[p]["body"]["State"] for p in ("before", "fault", "resumed")]
        check("resumed_container_running", resumed_state.get("Paused") is False
            and resumed_state.get("Running") is True and resumed_state.get("Status") == "running")
        if mode == "timeout":
            check("pause_same_process", fault_state.get("Paused") is True and fault_state.get("Status") == "paused"
                and before_state["StartedAt"] == fault_state["StartedAt"] == resumed_state["StartedAt"])
            last_at = epoch(old["growth_input"]["stream_last_valid_at"])
            check("timeout_after_established_stream", hold_at - last_at > 30)
            late = eligible_late_frames(raw, old, inspections["resumed"]["observed_at"], after["observed_at"])
            check("fresh_late_frame_did_not_unlock", bool(late) and at(after) - min(at(item) for item in late) >= 31)
        else:
            restart_at = epoch(fault_state["StartedAt"])
            check("actual_restart", epoch(before_state["StartedAt"]) < restart_at <= at(inspections["fault"])
                and fault_state["StartedAt"] == resumed_state["StartedAt"])
            errors = [item for item in raw if item["message"].get("name") == "growth_rate.status"
                and item["message"]["payload"].get("status") == "error"
                and item["message"]["payload"].get("requires_new_run") is True
                and item["message"]["payload"].get("restart_notification") is True
                and item["message"]["payload"].get("enabled") is False
                and item["message"]["payload"].get("measurement_ready") is False
                and all(item["message"]["payload"].get(key) == old["growth_input"].get(key)
                    for key in ("control_revision", "control_event_id", "initialization_generation", "alignment_revision"))
                and item["message"]["payload"].get("frame_seq", -1) >= baseline_sample["frame_seq"]
                and restart_at <= epoch(item["message"]["payload"]["occurred_at"]) <= hold_at
                and restart_at <= at(item) <= at(after)]
            check("trusted_restart_error_on_wire", bool(errors))
            samples_after = [item for item in raw if item["message"].get("name") == "growth_rate.sample"
                and restart_at <= epoch(item["message"]["payload"]["processed_at"]) <= at(after)]
            check("no_old_stream_restart", not samples_after, failure="FAIL")
            producer = capture("gsensor", "after")["body"]
            check("producer_requires_new_run", producer.get("current_run_id") == document["run_id"]
                and same_start(producer.get("experiment_started_at"), document["experiment_started_at"])
                and producer.get("active") is False and producer.get("measurement_ready") is False
                and producer.get("experiment_lifecycle_status") == "error"
                and (producer.get("recovery") or {}).get("requires_new_run") is True)
        check("new_run_live_G_recovery", bool(new.get("current_run_id")) and new["current_run_id"] != document["run_id"]
            and epoch(document["experiment_started_at"]) < epoch(new["started_at"]) <= at(recovered)
            and fresh_growth(new, recovered["observed_at"]))
        new_sample = new.get("last_valid_sample") or {}
        check("recovery_raw_frame", any(item["message"]["payload"].get("run_id") == new.get("current_run_id")
            and same_start(item["message"]["payload"].get("experiment_started_at"), new.get("started_at"))
            and all(item["message"]["payload"].get(key) == new_sample.get(key) for key in sample_fields)
            and at(after) < at(item) <= at(recovered) for item in all_samples))
        ended = capture("central", "ended")
        check("old_run_ended_before_new_run", ended["body"].get("run_id") == document["run_id"]
            and ended["body"].get("status") in {"completed", "error"}
            and at(after) <= at(ended) < epoch(new["started_at"]), failure="FAIL")
        for phase, status, sample in (("before", old, baseline_sample), ("recovered", new, new_sample)):
            activation = capture("activation", phase)
            command = activation["body"]["request"]
            check(phase + "_normal_activation_and_warmup", command.get("run_id") == status.get("current_run_id")
                and command.get("event_id") == status["growth_input"].get("control_event_id")
                and command.get("enabled") is True
                and epoch(status["started_at"]) <= epoch(activation["body"]["requested_at"])
                and epoch(sample["captured_at"]) - epoch(activation["body"]["requested_at"]) >= 120)
            config = (status.get("runtime_controls") or {}).get("configuration") or {}
            runtime = capture("runtime", phase)["body"]
            result = runtime.get("result") or {}
            check(phase + "_normal_committed_G_control", config.get("control_target") == "G"
                and config.get("adaptation_enabled") is False)
            check(phase + "_G_switch_committed_before_baseline", runtime.get("changes") == {"control_target": "G"}
                and result.get("status") == "applied" and result.get("refreshed") is True
                and type(result.get("effective_tick")) is int
                and status["last_control_output"]["tick_seq"] > result["effective_tick"]
                and (status["last_control_output"].get("runtime_configuration") or {}).get("control_target") == "G")
        check("clean_owned_shutdown", document.get("cleanup_complete") is True)
        check("both_archives_verified", document.get("archive_statuses") == ["PASS", "PASS"])
    except (KeyError, TypeError, ValueError) as exc:
        checks["required_evidence"] = {"status": "INCOMPLETE", "detail": str(exc)}
    states = [item["status"] for item in checks.values()]
    return {"case": "growth_" + str(document.get("mode")),
        "status": "FAIL" if "FAIL" in states else "INCOMPLETE" if "INCOMPLETE" in states else "PASS",
        "checks": checks, "paper_experiment": "NOT_RUN", "camera_hardware": "NOT_RUN"}
