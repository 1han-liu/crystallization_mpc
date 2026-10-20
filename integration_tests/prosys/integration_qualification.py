"""Host-only, hash-bound qualification of the named real Prosys fault captures.

No fault is injected here. Building is offline; live verification only reads
Podman metadata and hashes files inside the five already-running app containers.
Expected acquisition outages remain artifact failures, never full-suite PASS.
This is local evidence verification, not cryptographic attestation of a capture.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crystallization_mpc.testing.prosys.acceptance import PaperAcceptance
from crystallization_mpc.testing.prosys.fault_evidence import evaluate
from crystallization_mpc.testing.prosys.growth_fault_evidence import evaluate_growth_fault
from crystallization_mpc.testing.prosys.raw_store import timestamp_ns
from crystallization_mpc.testing.prosys.runner import (
    ProsysRunner, RunnerConfig, RunnerError, epoch, same_start, source_manifest,
    utc_now, validate_archive, verified_write, archive_experiment_outcome,
)


ROOT = Path(__file__).resolve().parents[2]
REPORTS = Path(".runtime/prosys-test/reports")
SCHEMA = "prosys_named_integration_qualification_v1"
SERVICES = {"central", "controller", "plant", "gsensor", "opcua-writer"}
TEST_DIFFERENCES = {
    "src/crystallization_mpc/testing/prosys/runner.py",
    "src/crystallization_mpc/testing/prosys/growth_fault_check.py",
    "src/crystallization_mpc/testing/prosys/growth_fault_evidence.py",
}
DEFAULT_CASES = {
    "static_read": "static-verify-002.json",
    "controller_restart": "controller-restart-003",
    "duplicate_command": "writer-duplicate-002",
    "stale_process": "stale-process-002",
    "broker_availability": "broker-availability-001",
    "growth_timeout": "growth-timeout-003",
    "gsensor_restart": "growth-restart-001",
    "model_stop": "model-stop-001",
    "native_disconnect": "native-disconnect-001",
    "read_only_setpoint": "native-readonly-001",
    "missing_node": "native-missing-count-001",
}
CSV_NAMES = ("parameters.csv", "process_data.csv", "controller_data.csv", "gsensor_data.csv", "events.csv")
TOOLS = ("integration_tests/prosys/integration_qualification.py", "integration_tests/prosys/paper_qualified_run.py",
    "integration_tests/prosys/native_fault_check.py", "integration_tests/prosys/model_stop_check.py",
    "integration_tests/prosys/native_disconnect_check.py")
HEX = re.compile(r"[0-9a-f]{64}\Z")


class QualificationError(RunnerError):
    pass


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def strict_json(content: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise QualificationError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    value = json.loads(content, object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(QualificationError(f"Nonfinite JSON: {value}")))
    if not isinstance(value, dict):
        raise QualificationError("Evidence JSON must be an object.")
    return value


def safe_path(root: Path, relative: str, *, reports_only=True) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise QualificationError("Evidence paths must be explicit repository-relative paths.")
    if reports_only and not path.is_relative_to(REPORTS):
        raise QualificationError("Evidence must stay in the isolated reports directory.")
    target = root / path
    # A symlink could change its target after hashing; reject every component.
    for part in (target, *target.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise QualificationError("Symlink evidence is not accepted.")
    if not target.resolve().is_relative_to(root.resolve()):
        raise QualificationError("Evidence escaped the repository.")
    return target


def file_ref(root: Path, path: Path) -> dict:
    relative = str(path.relative_to(root))
    target = safe_path(root, relative)
    content = target.read_bytes()
    return {"path": relative, "sha256": digest(content), "bytes": len(content)}


def read_ref(root: Path, reference: dict) -> bytes:
    if set(reference) != {"path", "sha256", "bytes"} or not HEX.fullmatch(str(reference["sha256"])):
        raise QualificationError("Malformed evidence file reference.")
    content = safe_path(root, reference["path"]).read_bytes()
    if len(content) != reference["bytes"] or digest(content) != reference["sha256"]:
        raise QualificationError(f"Evidence hash/size changed: {reference['path']}")
    return content


def tool_hashes(root: Path) -> dict:
    return {name: digest(safe_path(root, name, reports_only=False).read_bytes()) for name in TOOLS}


def manifest_valid(manifest: dict) -> bool:
    files = manifest.get("files")
    return (isinstance(files, dict) and bool(files) and all(HEX.fullmatch(str(value)) for value in files.values())
        and digest(json.dumps(files, sort_keys=True).encode()) == manifest.get("source_sha256"))


def same_production(captured: dict, current: dict) -> None:
    if not manifest_valid(captured) or not manifest_valid(current):
        raise QualificationError("Source manifest digest is invalid or missing.")
    old, new = captured["files"], current["files"]
    if set(old) != set(new) or any(old[name] != new[name] for name in old if name not in TEST_DIFFERENCES):
        raise QualificationError("Captured production source/configuration differs from the frozen source.")


def verify_provenance(deployment: dict, comparison: dict, current: dict) -> dict:
    same_production(comparison.get("host_manifest") or {}, current)
    if comparison.get("host_source_config_sha256") != current["source_sha256"]:
        raise QualificationError("Source comparison is not for this complete host source/configuration.")
    files = comparison.get("container_files") or {}
    expected = {name: value for name, value in current["files"].items()
        if name.startswith("src/") and (name.endswith(".py") or name.endswith("/app.js"))}
    if (set(files) != set(expected) or comparison.get("file_count") != len(expected)
            or any(not HEX.fullmatch(str(value)) for value in files.values())):
        raise QualificationError("Runtime source comparison does not cover every package/UI source file.")
    differences = {name for name in files if files[name] != expected[name]}
    if not differences <= TEST_DIFFERENCES:
        raise QualificationError("The app image differs in production source.")
    recorded = comparison.get("differences") or []
    if len(recorded) != len(differences) or {item.get("path") for item in recorded} != differences or any(
            item.get("host_sha256") != expected[item["path"]] or item.get("container_sha256") != files[item["path"]]
            for item in recorded):
        raise QualificationError("Recorded image/source differences contradict their hash maps.")
    containers = deployment.get("containers") or []
    if len(containers) != 5 or {item.get("compose_service") for item in containers} != SERVICES:
        raise QualificationError("Deployment must identify exactly the five isolated app services.")
    if len({item.get("id") for item in containers}) != 5 or any(
            not HEX.fullmatch(str(item.get("id"))) or item.get("image") != comparison.get("image_id")
            or item.get("compose_project") != "mpcrystal-prosys" or item.get("state") != "running" for item in containers):
        raise QualificationError("Deployment identity/image/project is inconsistent.")
    if comparison.get("container_id") not in {item["id"] for item in containers} or not HEX.fullmatch(str(comparison.get("image_id"))):
        raise QualificationError("Source comparison has no matching deployed app identity.")
    return {"status": "PASS", "image_id": comparison["image_id"], "containers": containers,
        "container_files": files, "host_only_differences": sorted(differences)}


def inspect_live(provenance: dict, *, run=subprocess.run) -> dict:
    """Read-only runtime check. Never creates, restarts, pauses, or execs control code."""
    ids = [item["id"] for item in provenance["containers"]]
    response = run(["podman", "inspect", *ids], check=True, capture_output=True, timeout=15)
    inspected = json.loads(response.stdout)
    if len(inspected) != 5 or {item.get("Id") for item in inspected} != set(ids):
        raise QualificationError("Live app container identities changed.")
    summaries = []
    for item in inspected:
        labels = item.get("Config", {}).get("Labels") or {}
        service = next(entry["compose_service"] for entry in provenance["containers"] if entry["id"] == item["Id"])
        expected = provenance["runtime_configuration"][service]
        environment = {}
        for value in item.get("Config", {}).get("Env", []):
            key, text = value.split("=", 1)
            if key in environment:
                raise QualificationError("Live environment has ambiguous duplicate keys.")
            environment[key] = text
        selected = {key: environment.get(key) for key in expected["environment_keys"]}
        labels_ok = all(any(labels.get(prefix + key) == value for prefix in ("io.podman.compose.", "com.docker.compose."))
            and not any(labels.get(prefix + key) not in (None, value) for prefix in ("io.podman.compose.", "com.docker.compose."))
            for key, value in (("project", "mpcrystal-prosys"), ("service", service)))
        if (item.get("Image", "").removeprefix("sha256:") != provenance["image_id"]
                or item.get("State", {}).get("Running") is not True or item.get("State", {}).get("Paused") is True
                or not labels_ok or digest(json.dumps(selected, sort_keys=True).encode()) != expected["environment_sha256"]
                or item.get("Config", {}).get("Cmd") != expected["command"] or item.get("Config", {}).get("Entrypoint") not in (None, [], "")
                or any(str(mount.get("Destination", "")).rstrip("/") in {"/app", "/app/src"}
                    or str(mount.get("Destination", "")).startswith("/app/src/") for mount in item.get("Mounts", []))):
            raise QualificationError("Live image, state, labels or source mounts differ from qualification.")
        # No imports: this subprocess only reads file bytes inside the image.
        auxiliary = ["/app/" + name for name in provenance.get("auxiliary_files", {})]
        script = ("import hashlib,json,pathlib; p=pathlib.Path('/app/src'); "
            "files=sorted(p.rglob('*.py'))+[p/'crystallization_mpc/apps/central/ui/static/app.js']+"
            f"[pathlib.Path(name) for name in {auxiliary!r}]; "
            "print(json.dumps({str(f.relative_to('/app')):hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.is_file()},sort_keys=True))")
        hashes = run(["podman", "exec", item["Id"], "python", "-B", "-c", script], check=True, capture_output=True, timeout=20)
        if json.loads(hashes.stdout) != {**provenance["container_files"], **provenance.get("auxiliary_files", {})}:
            raise QualificationError("Live app file bytes differ from the recorded image comparison.")
        summaries.append({"id": item["Id"], "service": service, "image_id": provenance["image_id"], "running": True})
    return {"status": "PASS", "observed_at": utc_now(), "containers": summaries,
        "scope": "Current runtime image/source identity only; not a retroactive deployment timestamp."}


def _capture(document: dict, kind: str, phase=None) -> dict:
    found = [item for item in document.get("captures", []) if item.get("kind") == kind
        and (phase is None or item.get("phase") == phase)]
    if len(found) != 1:
        raise QualificationError(f"Exactly one {kind}/{phase} capture is required.")
    epoch(found[0]["observed_at"])
    return found[0]


def _wire_checks(document: dict) -> None:
    items = [item for item in document.get("captures", []) if "raw_utf8" in item]
    items += document.get("wires", []) + document.get("raw_messages", [])
    for item in items:
        raw = item.get("raw_utf8")
        if raw is None:
            continue
        if digest(raw.encode()) != item.get("wire_sha256", item.get("sha256")) or strict_json(raw.encode()) != item.get("body", item.get("message")):
            raise QualificationError("Captured wire bytes, hash and parsed envelope disagree.")


def captured_identities(evidence: dict) -> set[tuple[str, float]]:
    identities = set()
    def take(value):
        if not isinstance(value, dict):
            return
        run = value.get("run_id", value.get("current_run_id"))
        start = value.get("experiment_started_at", value.get("started_at"))
        if isinstance(run, str) and run and start:
            identities.add((run, epoch(start)))
    take(evidence)
    for item in evidence.get("captures", []):
        body = item.get("body") or {}
        take(body)
        for key in ("identity", "controller", "plant", "experiment"):
            take(body.get(key))
    return identities


def static_read(document: dict, telemetry_path: Path) -> ProsysRunner:
    # Reuse the actual pure persisted-row checker, without creating an EvidenceLog
    # or calling preflight/prepare or opening any network connection.
    runner = object.__new__(ProsysRunner)
    runner.config = RunnerConfig(output=Path("unused"), commissioning_evidence=Path("unused"), telemetry_config=telemetry_path)
    runner.server_identity = None
    runner.gates = PaperAcceptance()
    class Log:
        def append(self, *args):
            pass
    runner.log = Log()
    # _load_commissioning opens the supplied path. The caller has already hashed
    # and parsed it; it supplies this exact reference rather than a temp rewrite.
    return runner


def _native_static(name: str, document: dict, directory: Path) -> dict:
    from integration_tests.prosys.native_fault_check import (
        check_archived_failure, check_failed_controller_row, check_readonly_wire,
    )
    if document.get("errors"):
        return {"status": "INCOMPLETE", "reason": "Native static collector recorded errors."}
    if name == "missing_node":
        fault = _capture(document, "missing_count_read")["body"]
        refusal = _capture(document, "five_node_client_refusal")["body"]
        if fault.get("code") != 2150891520 or refusal.get("connected") is not False or refusal.get("error_type") != "BadNodeIdUnknown":
            raise QualificationError("Missing-node evidence is not an actual BadNodeIdUnknown refusal.")
        for key in ("T", "T_j", "c", "T_j_set"):
            node = _capture(document, "present_node_" + key)["body"]
            if node.get("type") != "Double" or node.get("quality") != "StatusCode(value=0)":
                raise QualificationError("Unchanged native nodes are not Good Double.")
        for kind in ("inactive_guard", "inactive_after"):
            value = _capture(document, kind)["body"]
            if value["controller"].get("active") or value["plant"].get("running"):
                raise QualificationError("Missing-node static test overlapped an active process.")
        return {"status": "PASS", "scope": "Static missing-node rejection only; no experiment started."}
    before = _capture(document, "nodes_before")["body"]
    after = _capture(document, "nodes_after")["body"]
    if before != after and any(before[key]["value"] != after[key]["value"] for key in before):
        raise QualificationError("Read-only fixture values changed.")
    if "CurrentWrite" in before["T_j_set"]["user_access"]:
        raise QualificationError("Setpoint was not read-only.")
    commands = [item["message"] for item in document.get("wires", []) if item["message"].get("name") == "process.setpoint.write"]
    receipts = [item["message"] for item in document.get("wires", []) if item["message"].get("name") == "process.setpoint.result"]
    if len(commands) != 1 or len(receipts) != 1:
        raise QualificationError("Read-only case lacks its unique actual command/result.")
    check_readonly_wire(commands[0], receipts[0], document["run_id"], document["started_at"])
    terminal = _capture(document, "terminal_static_failure")
    check_failed_controller_row(terminal["body"], commands[0])
    observations = [item for item in document["captures"] if item["kind"] == "noresume_observation"]
    if not observations or epoch(observations[-1]["observed_at"]) - epoch(terminal["observed_at"]) < 30:
        raise QualificationError("Read-only no-resume observation is too short.")
    if any(item["body"].get("active") or item["body"].get("last_control_output") != terminal["body"].get("last_control_output") for item in observations):
        raise QualificationError("Read-only old run resumed or changed output.")
    check_archived_failure(directory / "static-run", commands[0])
    return {"status": "PASS", "run_id": document["run_id"], "started_at": document["started_at"]}


def _supervised(name: str, document: dict) -> dict:
    from integration_tests.prosys.model_stop_check import verify_frozen_rounds
    if document.get("errors") or not document.get("captures"):
        return {"status": "FAIL" if any(item.get("unsafe_observation") for item in document.get("errors", [])) else "INCOMPLETE",
            "reason": "Collector did not complete without errors."}
    if name == "model_stop":
        intent, stopped, rejected = [_capture(document, kind) for kind in
            ("model_stop_intent", "model_stop_response", "supervisor_rejected_model")]
        identity = intent["body"]["identity"]
        if (intent["body"].get("attempt") != 1 or stopped["body"].get("running") is not False
                or stopped["body"].get("status") != "stopped" or stopped["body"].get("run_id") != identity["run_id"]
                or not same_start(stopped["body"].get("experiment_started_at"), identity["experiment_started_at"])
                or not epoch(intent["observed_at"]) <= epoch(stopped["observed_at"]) <= epoch(rejected["observed_at"])
                or rejected["body"].get("reason") != "Test plant stopped or reported an error."
                or not isinstance(rejected["body"].get("accepted_ticks_before"), list)
                or rejected["body"].get("accepted_ticks_before") != rejected["body"].get("accepted_ticks_after")):
            raise QualificationError("Model stop/rejection identities or accepted ticks contradict the claim.")
        required = {"supervisor_rejected_and_ended_owned_run", "fresh_telegraf_does_not_mean_fresh_physics",
            "no_old_resume_two_periods", "distinct_new_run_recovered", "owned_archives_and_commands"}
    else:
        # Native collector already captures exact /proc+listener+project guards;
        # require their raw captures in addition to recorded collector checks.
        required = {"native_model_fault_rejected_and_old_run_ended", "same_project_new_native_pid_and_later_ua_start",
            "three_actual_fresh_good_reads_after_native_restart", "old_run_did_not_resume_for_two_periods", "distinct_new_experiment_verified_write"}
        if document.get("native_term_attempted") is not True or document.get("native_launch_attempted") is not True:
            raise QualificationError("Native outage has no actual guarded TERM and native launch.")
        if not document.get("project_sha256") or document.get("project_sha256") != document.get("project_sha256_at_end"):
            raise QualificationError("Native project changed across the actual outage.")
        before = _capture(document, "native_before")["body"]
        after = _capture(document, "native_restored")["body"]
        if (before["process"].get("pid") == after["process"].get("pid")
                or before["process"].get("project_sha256") != after["process"].get("project_sha256")
                or epoch(after["native"]["server"]["server_start_time"]) <= epoch(before["native"]["server"]["server_start_time"])):
            raise QualificationError("Native outage lacks a distinct PID, same project and later UA start time.")
        intent = _capture(document, "native_term_intent")
        rejected = _capture(document, "actual_runner_rejection")
        if (intent["body"].get("attempt") != 1 or epoch(rejected["observed_at"]) <= epoch(intent["observed_at"])
                or rejected["body"].get("real_native_model_fault") is not True
                or not isinstance(rejected["body"].get("accepted_before"), list)
                or rejected["body"].get("accepted_before") != rejected["body"].get("accepted_after")):
            raise QualificationError("Actual native runner rejection is not a non-advancing post-TERM observation.")
    if not required <= set(document.get("checks") or {}) or any(document["checks"][key] != "PASS" for key in required):
        return {"status": "INCOMPLETE", "reason": "Required supervised boundary checks are missing."}
    # Raw End records must prove both distinct identities are stopped. The
    # recovery output must also correlate to its normal production receipt.
    ended = [item for item in document["captures"] if item["kind"] == "end_confirmed"]
    identities = []
    for item in ended:
        body = item["body"]
        identity = body["identity"]
        status = body["controller"]
        if (status.get("active") is not False or status.get("status") != "stopped"
                or status.get("current_run_id") != identity["run_id"] or not same_start(status.get("started_at"), identity["experiment_started_at"])):
            raise QualificationError("Owned End confirmation is inconsistent.")
        identities.append(identity)
    if len(identities) != 2 or identities[0]["run_id"] == identities[1]["run_id"]:
        raise QualificationError("Both distinct owned experiments were not ended.")
    frozen = _capture(document, "fresh_reads_of_frozen_physics" if name == "model_stop" else "native_recovery_fresh_telegraf")["body"]
    boundary = stopped["observed_at"] if name == "model_stop" else after["native"]["verified_at"]
    verify_frozen_rounds(frozen["proof"], frozen["proof"]["node_ids"], boundary)
    noresume = [item for item in document["captures"] if item["kind"] == "no_old_resume"]
    if (not noresume or epoch(noresume[-1]["observed_at"]) - epoch(ended[0]["observed_at"]) < 30):
        raise QualificationError("Stopped old run lacks two periods of actual no-resume observations.")
    baseline = ended[0]["body"]["controller"]
    for item in noresume:
        status = item["body"] if name == "model_stop" else item["body"]["controller"]
        if (status.get("current_run_id") != identities[0]["run_id"] or not same_start(status.get("started_at"), identities[0]["experiment_started_at"])
                or status.get("status") != "stopped" or status.get("active") is not False
                or status.get("last_control_output") != baseline.get("last_control_output")
                or status.get("scheduler", {}).get("tick_count") != baseline.get("scheduler", {}).get("tick_count")):
            raise QualificationError("Old Controller output/clock changed after End.")
    verified = [item for item in document["captures"] if item["kind"] == "verified_ticks"]
    if len(verified) < 2:
        raise QualificationError("Both fault baseline and distinct recovery need actual verified ticks.")
    for item in verified:
        body = item["body"]
        status, identity = body["controller"], body["identity"]
        process = status["integrations"]["process"]
        output = status["last_control_output"]
        if (status.get("current_run_id") != identity["run_id"] or not same_start(status.get("started_at"), identity["experiment_started_at"])
                or output.get("run_id") != identity["run_id"] or (output.get("result") or {}).get("valid") is not True
                or output.get("tick_seq") not in body.get("ticks", []) or process.get("sample_valid") is not True
                or not verified_write({"command": process.get("last_command"), "receipt": process.get("last_write_result"), "write": output.get("process_write")},
                run_id=identity["run_id"], started_at=identity["experiment_started_at"])):
            raise QualificationError("Verified tick has no matching actual write/readback receipt.")
    if (verified[0]["body"]["identity"] != identities[0] or verified[-1]["body"]["identity"] != identities[1]
            or len(verified[0]["body"].get("ticks", [])) < 2):
        raise QualificationError("Fault baseline/recovery ticks do not belong to the two ended experiments.")
    return {"status": "PASS", "identities": identities}


def _outage_window(name: str, evidence: dict) -> tuple[int, int] | None:
    if name == "stale_process":
        before, after = _capture(evidence, "telegraf", "before"), _capture(evidence, "telegraf", "resumed")
        paused = _capture(evidence, "telegraf", "paused")
        if paused["body"].get("State", {}).get("Paused") is not True:
            raise QualificationError("Acquisition outage lacks an actual paused collector.")
        return timestamp_ns(before["observed_at"]), timestamp_ns(after["observed_at"])
    if name == "native_disconnect":
        # Actual native process timestamps are evaluated by the native tool;
        # generic unknown outage windows are deliberately not accepted here.
        terms = [item for item in evidence.get("captures", []) if item["kind"] == "native_term_intent"]
        restored = [item for item in evidence.get("captures", []) if item["kind"] == "native_restored"]
        if len(terms) != 1 or len(restored) != 1:
            raise QualificationError("No unambiguous native acquisition-outage window.")
        return timestamp_ns(terms[0]["observed_at"]), timestamp_ns(restored[0]["observed_at"])
    return None


def archive_review(root: Path, directory: Path, report: dict, name: str, evidence: dict) -> dict:
    from integration_tests.prosys.native_disconnect_check import analyze_outage_archive
    run_id, start = report["identity"]["run_id"], report["identity"]["experiment_started_at"]
    document = report.get("archive") or {}
    archive = document.get("archive") or {}
    coverage = archive.get("coverage") or {}
    outcome = archive_experiment_outcome(document, started_at=start, capture=document.get("capture") or {})
    content = {file: (directory / file).read_bytes() for file in CSV_NAMES}
    files = {file: {"sha256": digest(data), "bytes": len(data), "rows": len(list(csv.DictReader(io.StringIO(data.decode("utf-8-sig")))))} for file, data in content.items()}
    for file, data in content.items():
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        if files[file]["rows"] != archive.get("rows", {}).get(file) or any(row.get("run_id") != run_id for row in rows):
            raise QualificationError("Archive CSV row counts or run identities contradict the manifest.")
        recorded = report.get("archive_validation", {}).get("files", {}).get(file)
        if recorded and recorded != files[file]:
            raise QualificationError("Actual CSV bytes differ from their original validation evidence.")
    if (document.get("run_id") != run_id or archive.get("run_id") != run_id
            or not same_start(document.get("started_at"), start)
            or archive.get("pending_reasons") or any(coverage.get(key, {}).get("status") != "available" for key in ("controller", "process_events"))):
        return {"status": "INCOMPLETE", "controller_events_status": "INCOMPLETE", "files": files, "reason": "Archive finalization or Controller/event coverage is incomplete."}
    audit = analyze_outage_archive(directory, run_id, start)
    if audit["status"] != "PASS":
        return {"status": "FAIL", "controller_events_status": audit["status"], "command_audit": audit, "files": files}
    controller_rows = list(csv.DictReader(io.StringIO(content["controller_data.csv"].decode("utf-8-sig"))))
    ticks = sorted(int(row["tick_seq"]) for row in controller_rows)
    watermark = coverage.get("controller", {}).get("expected_last_sequence")
    if type(watermark) is not int or ticks != list(range(1, watermark + 1)):
        raise QualificationError("Controller archive does not match its actual finalization watermark.")
    # Revalidate ordinary completeness even if the report said PASS.
    try:
        valid = validate_archive(document, content, run_id=run_id, started_at=start,
            node_ids=report["server_identity"]["node_ids"], observed_ticks=set(), verified_commands=set(),
            require_g=name in {"growth_timeout", "gsensor_restart"})
        return {"status": "PASS", **outcome, "controller_events_status": "PASS", "command_audit": audit, "files": files, "validation": valid}
    except RunnerError as exc:
        reason = str(exc)
    window = _outage_window(name, evidence)
    process = coverage.get("process") or {}
    # Named exception: no missing Controller/event outcomes, only actual raw
    # acquisition gaps bounded by the captured injected outage (+one 1s round).
    if (window is None or archive.get("status") != "partial"
            or {item.get("code") for item in archive.get("errors", [])} != {"process_data_incomplete"}
            or process.get("incomplete_rounds") != 0 or not process.get("gaps")):
        return {"status": "FAIL", "controller_events_status": "PASS", "eligible": False, "files": files, "reason": reason}
    rows = list(csv.DictReader(io.StringIO(content["process_data.csv"].decode("utf-8-sig"))))
    stamps = [int(row["time_ns"]) for row in rows]
    lower, upper = int(document["capture"]["start_ns"]), int(document["capture"]["end_ns"])
    actual = [(a, b) for a, b in zip([lower, *stamps], [*stamps, upper]) if b - a > 1_500_000_000]
    expected = [(item["start_ns"], item["end_ns"]) for item in process["gaps"]]
    if (not stamps or stamps != sorted(set(stamps)) or actual != expected
            or any(not window[0] - 1_500_000_000 <= a < b <= window[1] + 1_500_000_000 for a, b in actual)
            or process.get("gap_count") != len(actual) or any(item.get("duration_ns") != item["end_ns"] - item["start_ns"] for item in process["gaps"])
            or any(row.get("complete", "").lower() != "true" or row.get("missing_fields") or row.get("invalid_fields")
                or row.get("device") != document["capture"].get("device") or row.get("temperature_unit") != "degC" for row in rows)):
        raise QualificationError("Raw acquisition gaps/invalid rows extend beyond the named proved fault.")
    # Do not waive node identity, quality, value or capture-boundary checks when
    # checking intentional collection gaps.
    for row in rows:
        if not lower <= int(row["time_ns"]) < upper:
            raise QualificationError("Raw collection lies outside capture.")
        for key, node in report["server_identity"]["node_ids"].items():
            if row.get(key + "_node_id") != node or row.get(key + "_quality") not in {"0", "OK (0x0)", "The operation succeeded. StatusGood (0x0)"}:
                raise QualificationError("Outage exception cannot waive node identity or quality.")
            if not __import__("math").isfinite(float(row[key])):
                raise QualificationError("Nonfinite raw value.")
    return {"status": "FAIL", **outcome, "controller_events_status": "PASS", "eligible": True,
        "limitation": "Expected acquisition outage only; original artifact FAIL retained.", "gaps": process["gaps"], "files": files, "command_audit": audit}


def _aggregate(values: list[str]) -> str:
    return "FAIL" if "FAIL" in values else "PASS" if values and all(value == "PASS" for value in values) else "INCOMPLETE"


def evaluate_bundle(bundle: dict, root: Path, *, current=None, live=False, live_probe=inspect_live) -> dict:
    current = source_manifest() if current is None else current
    if bundle.get("schema") != SCHEMA or set(bundle.get("cases", {})) != set(DEFAULT_CASES):
        raise QualificationError("Qualification schema or exact required case matrix differs.")
    if bundle.get("tool_hashes") != tool_hashes(root):
        raise QualificationError("Host qualification/wrapper tools changed.")
    if bundle.get("host_source") != current:
        raise QualificationError("Qualification belongs to another complete host source/configuration.")
    documents = {}
    for reference in bundle.get("files", []):
        if reference["path"] in documents:
            raise QualificationError("Duplicate evidence reference.")
        data = read_ref(root, reference)
        if reference["path"].endswith(".json"):
            documents[reference["path"]] = strict_json(data)
        else:
            documents[reference["path"]] = None
    def document(path):
        if path not in documents or documents[path] is None:
            raise QualificationError("A JSON dependency was not hash-bound.")
        return documents[path]
    provenance = verify_provenance(document(bundle["deployment"]), document(bundle["comparison"]), current)
    provenance["auxiliary_files"] = {name: current["files"][name] for name in ("params_default.yaml", "param_meta.yaml") if name in current["files"]}
    import yaml
    runtime_config = yaml.safe_load((root / ".runtime/prosys-test/compose.yaml").read_text())["services"]
    provenance["runtime_configuration"] = {}
    for service in SERVICES:
        environment = {key: str(value) for key, value in runtime_config[service]["environment"].items()}
        provenance["runtime_configuration"][service] = {"environment_keys": sorted(environment),
            "environment_sha256": digest(json.dumps(environment, sort_keys=True).encode()),
            "command": runtime_config[service]["command"]}
    outcomes = {}
    for name, case in bundle["cases"].items():
        if case.get("missing"):
            outcomes[name] = {"safety_status": "NOT_RUN", "artifact_status": "NOT_RUN", "eligible": False, "reason": case["missing"]}
            continue
        try:
            report = document(case["result"])
            case_directory = safe_path(root, case["directory"])
            if name != "static_read" and (Path(case["result"]).parent != Path(case["directory"])
                    or Path(case["evidence"]).parent != Path(case["directory"])):
                raise QualificationError("Case result/evidence must belong to the same explicit directory.")
            if name == "static_read":
                stub = static_read(report, root / ".runtime/prosys-test/telemetry.json")
                stub.config = RunnerConfig(output=Path("unused"), commissioning_evidence=root / case["result"], telemetry_config=root / ".runtime/prosys-test/telemetry.json")
                stub._load_commissioning()
                if report.get("action") != "verify-static":
                    raise QualificationError("Only verify-static is a read qualification.")
                outcomes[name] = {"safety_status": "PASS", "artifact_status": "PASS", "eligible": True, "scope": "Actual static raw Influx/Telegraf read path; direct UA writes are not Writer acceptance."}
                continue
            evidence = document(case["evidence"])
            same_production(evidence.get("source_manifest") or {}, current)
            same_production(evidence.get("source_manifest_at_end") or {}, current)
            if evidence["source_manifest"]["source_sha256"] != evidence["source_manifest_at_end"]["source_sha256"]:
                raise QualificationError("Source changed during the captured case.")
            _wire_checks(evidence)
            harness_name = {"model_stop": "model_stop_check.py", "read_only_setpoint": "native_fault_check.py", "missing_node": "native_fault_check.py"}.get(name)
            if harness_name:
                expected_hash = digest((root / "integration_tests/prosys" / harness_name).read_bytes())
                if evidence.get("host_harness_sha256") != expected_hash or evidence.get("host_harness_sha256_at_end", expected_hash) != expected_hash:
                    raise QualificationError("Captured host collector differs from the explicit current verifier dependency.")
            if name == "native_disconnect":
                captured_tools = evidence.get("host_harness_hashes") or {}
                required_tools = {"integration_tests/prosys/" + item for item in ("native_disconnect_check.py", "model_stop_check.py", "native_fault_check.py")}
                if (set(captured_tools) != required_tools or captured_tools != evidence.get("host_harness_hashes_at_end")
                        or any(value != digest(safe_path(root, path, reports_only=False).read_bytes()) for path, value in captured_tools.items())):
                    raise QualificationError("Native capture collector/helper hashes changed or are incomplete.")
            if name in {"controller_restart", "duplicate_command", "stale_process", "broker_availability"}:
                if name == "duplicate_command":
                    commands = [item for item in evidence.get("captures", []) if item.get("kind") == "command" and item.get("phase") in {"original", "replay"}]
                    if len(commands) != 2 or not commands[0].get("raw_utf8") or commands[0].get("raw_utf8") != commands[1].get("raw_utf8"):
                        raise QualificationError("Duplicate test must capture exactly the same original/replayed wire bytes.")
                reevaluated = evaluate(name, evidence)
            elif name in {"growth_timeout", "gsensor_restart"}:
                if evidence.get("mode") != ("timeout" if name == "growth_timeout" else "restart"):
                    raise QualificationError("Growth fault mode differs from its matrix identity.")
                reevaluated = evaluate_growth_fault(evidence)
            elif name in {"read_only_setpoint", "missing_node"}:
                reevaluated = _native_static(name, evidence, case_directory)
            else:
                reevaluated = _supervised(name, evidence)
            # A partial raw gap may make the native overall INCOMPLETE, but its
            # separately recorded safety status must also be PASS.
            declared = report.get("safety_status", report.get("status"))
            safety = reevaluated["status"]
            if declared != "PASS" and safety == "PASS":
                safety = "FAIL" if declared == "FAIL" else "INCOMPLETE"
            archives = []
            for path in case.get("archives", []):
                archive = document(path)
                if any(str(Path(path).parent / file) not in documents for file in CSV_NAMES):
                    archives.append({"status": "INCOMPLETE", "controller_events_status": "INCOMPLETE",
                        "reason": "Not all five CSVs exist and are hash-bound; no completed archive claim."})
                    continue
                same_production(archive.get("source_manifest") or {}, current)
                same_production(archive.get("source_manifest_at_end") or {}, current)
                if archive["source_manifest"]["source_sha256"] != archive["source_manifest_at_end"]["source_sha256"]:
                    raise QualificationError("Source changed during the archived experiment.")
                identity = archive.get("identity") or {}
                if (identity.get("run_id"), epoch(identity.get("experiment_started_at"))) not in captured_identities(evidence):
                    raise QualificationError("Archive experiment was not captured by this fault test.")
                archives.append(archive_review(root, root / Path(path).parent, archive, name, evidence))
            required_archives = 0 if name in {"duplicate_command", "missing_node"} else 1 if name in {"controller_restart", "read_only_setpoint"} else 2
            if len(archives) != required_archives:
                artifacts, eligible = "INCOMPLETE", False
            else:
                artifacts = _aggregate([item["status"] for item in archives]) if archives else "PASS"
                eligible = all(item.get("controller_events_status") == "PASS" and (item["status"] == "PASS" or item.get("eligible") is True) for item in archives)
            outcomes[name] = {"safety_status": safety, "artifact_status": artifacts,
                "eligible": safety == "PASS" and eligible, "recomputed": reevaluated, "archives": archives,
                "original_status": report.get("status"), "evidence": case["evidence"]}
        except (OSError, KeyError, ValueError, TypeError, AttributeError, RunnerError) as exc:
            outcomes[name] = {"safety_status": "FAIL", "artifact_status": "INCOMPLETE", "eligible": False, "reason": str(exc)}
    safety = _aggregate([item["safety_status"] for item in outcomes.values()])
    artifacts = _aggregate([item["artifact_status"] for item in outcomes.values()])
    runtime = live_probe(provenance) if live else {"status": "NOT_RUN", "scope": "Offline build; current runtime is not claimed."}
    eligible = all(item["eligible"] for item in outcomes.values()) and (not live or runtime["status"] == "PASS")
    return {"safety_status": safety, "artifact_status": artifacts, "provenance_status": "PASS",
        "overall": "FAIL" if safety == "FAIL" else "PASS" if safety == artifacts == "PASS" else "INCOMPLETE",
        "eligible_for_paper": eligible, "runtime": runtime, "cases": outcomes,
        "limitations": ["Named scenarios only; broker in-flight write delivery is NOT_RUN.",
            "Real camera/eight-image warmup, FBRM, secondary nucleation and hardware are NOT_RUN.",
            "Expected acquisition-outage artifact failures remain failures; no generic ignore-artifacts option."],
        "source_scope": "Production image/source equivalence; explicitly listed host-only collector differences retained."}


def build_bundle(root: Path, *, cases=None, deployment=REPORTS / "deployment-002.json",
                 comparison=REPORTS / "runtime-source-comparison-002.json", current=None) -> dict:
    paths = dict(DEFAULT_CASES)
    if cases:
        if set(cases) - set(paths):
            raise QualificationError("Unknown qualification case override.")
        paths.update(cases)
    current = source_manifest() if current is None else current
    references, entries = {}, {}
    def include(path):
        target = safe_path(root, str(path))
        references[str(path)] = file_ref(root, target)
        return strict_json(target.read_bytes()) if target.suffix == ".json" else None
    include(deployment)
    include(comparison)
    for name, supplied in paths.items():
        relative = REPORTS / supplied
        directory = relative.parent if name == "static_read" else relative
        result = relative if name == "static_read" else relative / "fault_result.json"
        if not safe_path(root, str(result)).is_file():
            entries[name] = {"missing": f"No completed explicit result at {result}"}
            continue
        value = include(result)
        entry = {"directory": str(directory), "result": str(result), "archives": []}
        if name != "static_read":
            evidence = relative / "fault_evidence.json"
            if not safe_path(root, str(evidence)).is_file():
                entries[name] = {"missing": f"No actual raw evidence at {evidence}"}
                continue
            include(evidence)
            collector_log = relative / "evidence.jsonl"
            if safe_path(root, str(collector_log)).is_file():
                include(collector_log)
            entry["evidence"] = str(evidence)
            expected = [] if name in {"duplicate_command", "missing_node"} else ["report.json"] if name == "controller_restart" else ["static-run/report.json"] if name == "read_only_setpoint" else ["fault-run/report.json", "recovery-run/report.json"]
            for archive_name in expected:
                report_path = relative / archive_name
                if safe_path(root, str(report_path)).is_file():
                    include(report_path)
                    entry["archives"].append(str(report_path))
                    for file in CSV_NAMES:
                        if safe_path(root, str(report_path.parent / file)).is_file():
                            include(report_path.parent / file)
                    archive_log = report_path.parent / "evidence.jsonl"
                    if safe_path(root, str(archive_log)).is_file():
                        include(archive_log)
            recovery = relative / "recovery-fixture.json"
            if safe_path(root, str(recovery)).is_file():
                include(recovery)
        entries[name] = entry
    bundle = {"schema": SCHEMA, "created_at": utc_now(), "host_source": current,
        "tool_hashes": tool_hashes(root), "deployment": str(deployment), "comparison": str(comparison),
        "cases": entries, "files": [references[key] for key in sorted(references)]}
    bundle["verification"] = evaluate_bundle(bundle, root, current=current)
    return bundle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--deployment", type=Path, default=REPORTS / "deployment-002.json")
    parser.add_argument("--comparison", type=Path, default=REPORTS / "runtime-source-comparison-002.json")
    parser.add_argument("--case", action="append", default=[], help="Explicit named case=report-directory override; never auto-select latest.")
    parser.add_argument("--live", action="store_true", help="Read-only current Podman image/source verification.")
    args = parser.parse_args(argv)
    if args.action == "build":
        if args.output is None or args.live or args.qualification is not None:
            parser.error("Offline build needs --output, without --live/--qualification.")
        overrides = {}
        for item in args.case:
            key, value = item.split("=", 1)
            if key in overrides:
                parser.error("Duplicate case override.")
            overrides[key] = value
        bundle = build_bundle(ROOT, cases=overrides, deployment=args.deployment, comparison=args.comparison)
        target = safe_path(ROOT, str(args.output))
        with target.open("x", encoding="utf-8") as stream:
            json.dump(bundle, stream, indent=2, allow_nan=False)
            stream.write("\n")
        outcome = bundle["verification"]
    else:
        if args.qualification is None or args.output or args.case:
            parser.error("Verify needs --qualification and optional --live only.")
        outcome = evaluate_bundle(strict_json(args.qualification.read_bytes()), ROOT, live=args.live)
    print(json.dumps({key: outcome[key] for key in ("overall", "safety_status", "artifact_status", "eligible_for_paper")}, sort_keys=True))
    return 0 if outcome["overall"] == "PASS" else 4 if outcome["eligible_for_paper"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
