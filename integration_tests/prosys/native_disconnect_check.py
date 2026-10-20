"""Explicit, bounded fault test of the actual owned native Prosys process.

Only --execute-disconnect plus an explicit verified PID authorizes one TERM.
The normal process watchdog is never bypassed. A later desktop launch is not
treated as recovery until actual PID, UA identity and fresh reads agree.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any

from crystallization_mpc.testing.prosys.duplicate_check import load_server_evidence
from crystallization_mpc.testing.prosys.opcua_client import ProsysProcessClient
from crystallization_mpc.testing.prosys.restart_check import save_exclusive
from crystallization_mpc.testing.prosys.runner import (
    EvidenceLog, HttpReadTimeout, JsonHttpClient, ProsysRunner, RunnerConfig, RunnerError,
    epoch, finite, same_start, source_manifest, utc_now, verified_write,
)
from crystallization_mpc.testing.prosys.stale_check import prepare_recovery_fixture
if __package__:
    from integration_tests.prosys.model_stop_check import BoundedHttpClient, verify_frozen_rounds
    from integration_tests.prosys.native_fault_check import node_snapshot, verify_server
else:  # Direct opt-in CLI: Python places this script's own directory on sys.path.
    from model_stop_check import BoundedHttpClient, verify_frozen_rounds
    from native_fault_check import node_snapshot, verify_server


MAIN_CLASS = "install4j.com.prosysopc.ua.app.Main"
BASELINE_SHA256 = "510160dc21584c4e965920f0e607ec64619eef4170152bb31798c94d020ec993"
PROJECT_RELATIVE = Path(".runtime/prosys-test/native-fixtures/normal-baseline-001.uasim")


class NativeSafetyFailure(RunnerError):
    """Actual unsafe behavior, distinct from missing/late evidence."""


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inactive_owned(proof: dict, run_id: str, started_at: str) -> None:
    controller, plant = proof.get("controller") or {}, proof.get("plant") or {}
    if (not isinstance(run_id, str) or not run_id or not isinstance(started_at, str)
            or not same_start(started_at, started_at)
            or controller.get("current_run_id") != run_id or not same_start(controller.get("started_at"), started_at)
            or controller.get("status") != "stopped" or controller.get("active") is not False
            or not controller.get("stopped_at") or plant.get("run_id") != run_id
            or not same_start(plant.get("experiment_started_at"), started_at)
            or plant.get("running") is not False or plant.get("status") not in {"stopped", "error"}):
        raise RunnerError("Both the exact old Controller and model must be confirmed inactive before native recovery.")


class NativeProcessGuard:
    """Narrow Linux /proc proof; never discovers a PID to kill on the user's behalf."""
    def __init__(self, workspace_root: Path, project: Path, *, proc_root=Path("/proc"), uid=None,
                 signal_sender=None, launcher=subprocess.Popen):
        self.root, self.proc_root = Path(workspace_root).resolve(), Path(proc_root)
        self.project = Path(project).absolute()
        self.uid = os.getuid() if uid is None else uid
        self.java = self.root / ".runtime/prosys-test/application/jre/bin/java"
        self.desktop = self.root / ".runtime/prosys-test/application/UaSimulationServer"
        self.classpath = [str(self.desktop.parent / ".install4j/i4jruntime.jar"),
            str(self.desktop.parent / ".install4j/launcher278c8ec0.jar"), str(self.desktop.parent / "lib/*")]
        self.signal_sender, self.launcher = signal_sender, launcher
        self.term_attempted = self.launch_attempted = False
        self._validate_project()

    def _validate_project(self):
        if (self.project != self.root / PROJECT_RELATIVE or self.project.is_symlink()
                or not self.project.is_file() or self.project.resolve() != self.project
                or file_hash(self.project) != BASELINE_SHA256):
            raise RunnerError("Only the unchanged explicit normal-baseline-001 Prosys project is permitted.")
        if not self.java.is_file() or self.java.is_symlink() or not self.desktop.is_file() or self.desktop.is_symlink():
            raise RunnerError("The exact bundled native Java and official desktop launcher are required.")
        if (any(not Path(path).is_file() or Path(path).is_symlink() for path in self.classpath[:2])
                or not (self.desktop.parent / "lib").is_dir() or (self.desktop.parent / "lib").is_symlink()):
            raise RunnerError("The two exact official launcher classpath JARs and application lib directory are required.")

    def _installation_hashes(self):
        return {"java_sha256": file_hash(self.java), "launcher_sha256": file_hash(self.desktop),
                "classpath_jar_hashes": {path: file_hash(Path(path)) for path in self.classpath[:2]}}

    def _stat(self, pid):
        if type(pid) is not int or pid <= 1 or pid == os.getpid():
            raise RunnerError("An explicit positive external native PID is required.")
        directory = self.proc_root / str(pid)
        content = (directory / "stat").read_text()
        match = re.fullmatch(r"(\d+) \(.*\) (.*)\n?", content)
        if not match or int(match[1]) != pid:
            raise RunnerError("Native process stat identity is malformed.")
        fields = match[2].split()
        uid_line = next((line.split()[1:] for line in (directory / "status").read_text().splitlines()
                         if line.startswith("Uid:")), [])
        if len(fields) < 20 or int(fields[19]) <= 0 or uid_line != [str(self.uid)] * 4:
            raise RunnerError("Native process start identity or owner UID is not proven.")
        return directory, fields[0], int(fields[19])

    def _listener_inodes(self, directory):
        listening = set()
        for name in ("tcp", "tcp6"):
            table = directory / "net" / name
            if not table.exists():
                continue
            for line in table.read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 9 and fields[3] == "0A" and fields[1].rsplit(":", 1)[-1].upper() == "D11A":
                    listening.add(fields[9])
        owned = set()
        for descriptor in (directory / "fd").iterdir():
            try:
                match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(descriptor))
            except FileNotFoundError:
                continue
            if match:
                owned.add(match[1])
        actual = sorted(listening & owned)
        if not actual:
            raise RunnerError("The explicit native PID does not own the actual OPC UA 53530 listening socket.")
        return actual

    def inspect(self, pid: int) -> dict:
        self._validate_project()
        directory, state, start_ticks = self._stat(pid)
        raw = (directory / "cmdline").read_bytes()
        args = raw.rstrip(b"\0").decode("utf-8", "strict").split("\0")
        exe = os.readlink(directory / "exe")
        official_classpath = (args.count("-classpath") == 1 and "-cp" not in args and "--class-path" not in args
            and args.index("-classpath") + 1 < len(args) and args[args.index("-classpath") + 1] == ":".join(self.classpath)
            and not any(arg.startswith(("--class-path=", "-Djava.class.path=", "-javaagent:", "-agentpath:", "-Xbootclasspath")) for arg in args))
        if (state in {"Z", "X"} or exe != str(self.java) or not args or args[0] != str(self.java)
                or args.count(MAIN_CLASS) != 1 or args.count("--project") != 1
                or args[-3:] != [MAIN_CLASS, "--project", str(self.project)] or not official_classpath):
            raise RunnerError("Explicit PID is not the exact owned native Java/Main/normal-project desktop process.")
        return {"pid": pid, "uid": self.uid, "start_ticks": start_ticks, "state": state,
            "exe": exe, "project": str(self.project), "project_sha256": file_hash(self.project),
            "cmdline_sha256": hashlib.sha256(raw).hexdigest(), "main_class": MAIN_CLASS,
            "classpath": list(self.classpath), **self._installation_hashes(),
            "listener_inodes": self._listener_inodes(directory)}

    def verify_same(self, expected: dict) -> dict:
        actual = self.inspect(expected["pid"])
        if any(actual[key] != expected.get(key) for key in
               ("pid", "uid", "start_ticks", "exe", "project", "project_sha256", "cmdline_sha256", "listener_inodes",
                "classpath", "java_sha256", "launcher_sha256", "classpath_jar_hashes")):
            raise RunnerError("Native PID identity changed; no signal may be sent.")
        return actual

    def exited(self, expected: dict) -> bool:
        try:
            _directory, state, start_ticks = self._stat(expected["pid"])
        except FileNotFoundError:
            return True
        if start_ticks != expected["start_ticks"]:
            raise RunnerError("PID was reused; native exit/recovery proof is ambiguous.")
        return state in {"Z", "X"}

    def terminate_once(self, expected: dict) -> dict:
        if self.term_attempted:
            raise RunnerError("The one native TERM was already attempted; never retry an unknown outcome.")
        if self.signal_sender is None:
            if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                raise RunnerError("Atomic pidfd signaling is required; refusing a PID-reuse-prone kill fallback.")
            descriptor = os.pidfd_open(expected["pid"], 0)
            try:
                actual = self.verify_same(expected)
                self.term_attempted = True
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            finally:
                os.close(descriptor)
        else:
            actual = self.verify_same(expected)
            self.term_attempted = True
            self.signal_sender(actual["pid"], signal.SIGTERM)
        return {"attempt": 1, "pid": actual["pid"], "start_ticks": actual["start_ticks"], "signal": "SIGTERM",
                "signal_sent": True, "exit_confirmed": False}

    def _native_pids(self):
        pids = []
        for directory in self.proc_root.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                if directory.stat().st_uid != self.uid:
                    continue  # Never inspect or interfere with another user's desktop process.
                if (directory / "comm").read_text().strip() != "java":
                    continue  # Inaccessible same-user systemd/browser processes are not a Java desktop candidate.
                if os.readlink(directory / "exe") != str(self.java):
                    continue
                args = (directory / "cmdline").read_bytes().split(b"\0")
                if MAIN_CLASS.encode() in args:
                    pids.append(int(directory.name))
            except FileNotFoundError:
                continue
        return pids

    def launch_once(self, log_path: Path, old_snapshot: dict, inactive_proof: dict) -> dict:
        if self.launch_attempted:
            raise RunnerError("Native desktop launch was already attempted; never duplicate an uncertain launch.")
        self._validate_project()
        installation = self._installation_hashes()
        if any(installation[key] != old_snapshot.get(key) for key in installation):
            raise RunnerError("The official native installation changed; refusing a different executable/classpath during recovery.")
        controller = inactive_proof.get("controller") or {}
        inactive_owned(inactive_proof, controller.get("current_run_id"), controller.get("started_at"))
        if not self.exited(old_snapshot):
            raise RunnerError("The exact old native process has not exited.")
        live = [pid for pid in self._native_pids()
                if pid != old_snapshot["pid"] or not self.exited(old_snapshot)]
        if live:
            raise RunnerError("Another native desktop process exists; do not launch or disturb it.")
        log_path = Path(log_path).absolute()
        runtime = self.root / ".runtime"
        if (runtime not in log_path.parents or runtime.resolve() not in log_path.resolve().parents
                or log_path.is_symlink() or any(parent.is_symlink() for parent in log_path.parents if parent != self.root)):
            raise RunnerError("Vendor output must go to a new private runtime file.")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.launch_attempted = True
        try:
            with os.fdopen(descriptor, "wb") as stream:
                child = self.launcher([str(self.desktop), "--project", str(self.project)],
                    cwd=str(self.desktop.parent), stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        except BaseException:
            # The log remains private evidence, including uncertain spawn failure.
            raise
        return {"attempt": 1, "launcher_pid": child.pid, "log_path": str(log_path),
            "log_mode": oct(log_path.stat().st_mode & 0o777), "project": str(self.project),
            "scope": "Launch requested only; neither exit status nor PID alone proves UA readiness."}

    def discover_new(self, old_snapshot: dict) -> dict:
        candidates = [self.inspect(pid) for pid in self._native_pids() if pid != old_snapshot["pid"]]
        if len(candidates) != 1 or candidates[0]["start_ticks"] <= old_snapshot["start_ticks"]:
            raise RunnerError("Exactly one later owned native process with the expected project/listener is required.")
        return candidates[0]


def probe_native(config, reference: dict) -> dict:
    """Read-only actual UA product, five-node contract and server-start evidence."""
    client = ProsysProcessClient(config)
    try:
        identity = client.connect()
        actual = verify_server(client._client, reference)
        snapshots = {key: node_snapshot(client._nodes[key]) for key in reference["node_ids"]}
        if any("CurrentWrite" not in item["user_access"] for item in snapshots.values()):
            raise RunnerError("Native recovery does not have five actual writable Good Double nodes.")
        return {"identity": identity, "server": actual, "nodes": snapshots, "verified_at": utc_now()}
    finally:
        client.close()


def analyze_outage_archive(directory: Path, run_id: str, started_at: str) -> dict:
    """Retain actual invalid attempts; never turn a missing write outcome into success."""
    result = {"status": "PASS", "valid_ticks": [], "failed_read_ticks": [], "failed_write_ticks": [],
              "invalid_commit_verified_ticks": [], "unattempted_failure_details": [], "issues": []}
    def issue(text, incomplete=False):
        result["issues"].append(text)
        if not incomplete or result["status"] == "PASS":
            result["status"] = "INCOMPLETE" if incomplete else "FAIL"
    try:
        with (directory / "controller_data.csv").open(encoding="utf-8-sig", newline="") as stream:
            controllers = list(csv.DictReader(stream))
        with (directory / "events.csv").open(encoding="utf-8-sig", newline="") as stream:
            events = list(csv.DictReader(stream))
        ticks, ids = set(), set()
        if not controllers:
            raise RunnerError("No Controller attempts were archived.")
        for row in controllers:
            tick = int(row["tick_seq"])
            if row.get("run_id") != run_id or tick < 1 or tick in ticks:
                raise RunnerError("Invalid/foreign/duplicate archived tick identity.")
            ticks.add(tick)
            valid, attempted = row.get("valid", "").lower(), row.get("process_write_attempted", "").lower()
            if valid not in {"true", "false"} or attempted not in {"true", "false"}:
                raise RunnerError("Actual valid/attempted booleans are missing.")
            related = [event for event in events if event.get("run_id") == run_id and event.get("tick_seq") == str(tick)]
            if valid == "false" and (not row.get("error") or row.get("diag_capture_error") != "invalid_output"
                    or row.get("diag_capture_ok", "").lower() not in {"", "false"}
                    or any(value for key, value in row.items() if key.startswith("diag_")
                           and key not in {"diag_capture_error", "diag_capture_ok"})):
                raise RunnerError("Failed attempt lacks its actual error or contains false diagnostic success evidence.")
            if attempted == "false":
                if valid == "true" or row.get("process_write_success", "").lower() == "true" or any((event.get("service"), event.get("stage")) in
                        {("controller", "prepared"), ("controller", "receipt"), ("opcua_writer", "result")} for event in related):
                    raise RunnerError("Unattempted failure contains a command/receipt or false valid flag.")
                result["failed_read_ticks"].append(tick)
                result["unattempted_failure_details"].append({"tick_seq": tick, "error": row["error"],
                    "scope": "Actual pre-write failure; read/validation/algorithm cause is retained verbatim, not inferred from a missing write."})
                continue
            prepared = [event for event in related if event.get("service") == "controller" and event.get("stage") == "prepared"]
            if len(prepared) != 1:
                raise RunnerError("Attempted output needs exactly one actual prepared command.")
            command = json.loads(prepared[0]["payload"])
            command_id = command.get("command_id")
            if (not command_id or command_id in ids or command_id != prepared[0].get("command_id")
                    or command.get("run_id") != run_id or not same_start(command.get("run_started_at"), started_at)
                    or type(command.get("tick_seq")) is not int or command["tick_seq"] != tick
                    or not finite(command.get("setpoint_K"))
                    or (valid == "true" or row.get("T_j_set")) and (not finite(float(row["T_j_set"]))
                        or abs(command["setpoint_K"] - float(row["T_j_set"])) > 1e-8)):
                raise RunnerError("Actual prepared command differs from the Controller attempt.")
            ids.add(command_id)
            receipts = []
            for service, stage in (("controller", "receipt"), ("opcua_writer", "result")):
                matching = [event for event in related if event.get("service") == service and event.get("stage") == stage
                            and event.get("command_id") == command_id]
                if not matching:
                    issue(f"Tick {tick}: {service}/{stage} outcome is unavailable, not a success.", incomplete=True)
                    continue
                if len(matching) != 1:
                    raise RunnerError("Duplicate actual receipt/result is ambiguous.")
                receipt = json.loads(matching[0]["payload"])
                if (any(receipt.get(key) != command.get(key) for key in ("command_id", "run_id", "run_started_at", "tick_seq"))
                        or receipt.get("status") != matching[0].get("status")):
                    raise RunnerError("Actual receipt/result identity differs from its command.")
                receipts.append(receipt)
            if len(receipts) != 2:
                continue
            if receipts[0] != receipts[1]:
                raise RunnerError("Controller and Writer disagree about the actual outcome.")
            receipt = receipts[0]
            verified = receipt.get("error") is None and verified_write(
                {"command": command, "receipt": receipt, "write": receipt.get("write_result")}, run_id=run_id, started_at=started_at)
            if verified:
                if row.get("process_write_success", "").lower() == "false":
                    raise RunnerError("Archived verified write conflicts with its explicit failed-write flag.")
                result["valid_ticks" if valid == "true" else "invalid_commit_verified_ticks"].append(tick)
            elif valid == "false" and receipt.get("status") in {"failed", "unknown"} and receipt.get("error") and receipt.get("write_result") is None:
                if row.get("process_write_success", "").lower() == "true":
                    raise RunnerError("Archived failed/unknown write has a false success flag.")
                result["failed_write_ticks"].append(tick)
            else:
                raise RunnerError("A failed/unknown/unverified production write was mislabeled as successful.")
        if ticks != set(range(1, max(ticks) + 1)):
            raise RunnerError("Archived failed-attempt sequence has a gap.")
        for event in events:
            if (event.get("run_id") == run_id and (event.get("service"), event.get("stage")) in
                    {("controller", "prepared"), ("controller", "receipt"), ("opcua_writer", "result")}
                    and event.get("command_id") not in ids):
                raise RunnerError("Archive has an orphan command with no attempted Controller cycle.")
    except (OSError, RunnerError, KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        issue(str(exc))
    return result


def run_native_disconnect_check(config: RunnerConfig, *, native_pid: int, project: Path,
        execute_disconnect: bool, runner_factory=ProsysRunner, client=None, process_guard=None,
        server_probe=probe_native, monotonic=time.monotonic, sleep=time.sleep, now=utc_now,
        source_provider=source_manifest, recovery_fixture=prepare_recovery_fixture,
        total_timeout_s: float = 300) -> dict:
    # No directory creation, source discovery, HTTP or /proc access before opt-in.
    if execute_disconnect is not True:
        raise ValueError("Explicit --execute-disconnect is required before any native process action.")
    if (type(native_pid) is not int or native_pid <= 1 or native_pid == os.getpid()
            or config.mode != "short-dynamic" or not finite(total_timeout_s) or not 120 <= total_timeout_s <= 300):
        raise ValueError("A positive explicit external native PID, short-dynamic mode and 120..300 s budget are required.")
    if config.commissioning_evidence is None:
        raise ValueError("The actual passing Prosys commissioning evidence must be explicit.")
    began = monotonic()
    native_config, reference, reference_file = load_server_evidence(config.commissioning_evidence)
    root = Path(__file__).resolve().parents[2]
    if process_guard is None and (root / ".runtime").resolve() not in config.output.absolute().resolve().parents:
        raise ValueError("Native-disconnect evidence and private vendor logs must use a new final/.runtime directory.")
    guard = process_guard or NativeProcessGuard(root, project)
    if process_guard is None and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RunnerError("No desktop display is available to restore the official Prosys application; refusing to interrupt it.")
    # Read-only proof also precedes creation of the experiment/evidence directory.
    old_native = guard.inspect(native_pid)
    before_native = server_probe(native_config, reference)
    guard.verify_same(old_native)
    if not same_start(before_native["server"]["server_start_time"], before_native["server"]["server_start_time"]):
        raise RunnerError("Actual native ServerStatus.StartTime is missing or malformed.")
    sources = source_provider()
    final_deadline = began + total_timeout_s
    http = BoundedHttpClient(client or JsonHttpClient(timeout_s=5), final_deadline - 45, monotonic)
    log = EvidenceLog(config.output, now)
    captures, errors, checks, artifacts, runners = [], [], {}, {}, []
    ended, end_attempted, stop_attempted, reported = {}, set(), set(), set()
    old = recovery = None
    restored = None
    launched = None
    term_attempted = False
    host_paths = [Path(__file__), Path(__file__).with_name("model_stop_check.py"), Path(__file__).with_name("native_fault_check.py")]
    host_before = {str(path.relative_to(root)): file_hash(path) for path in host_paths}

    def capture(kind, body):
        item = {"kind": kind, "observed_at": now(), "body": deepcopy(body)}
        captures.append(item)
        log.append(kind, item)
        return item

    def failure(exc, phase):
        item = {"phase": phase, "type": type(exc).__name__, "message": str(exc),
                "unsafe_observation": isinstance(exc, NativeSafetyFailure)}
        errors.append(item)
        log.append("native_disconnect_error", item)

    def remaining(reserve=0):
        if http.deadline - monotonic() <= reserve:
            raise TimeoutError("Insufficient native-disconnect budget; no next mutation is attempted.")

    def pause(seconds):
        remaining()
        sleep(min(seconds, max(0., http.deadline - monotonic())))
        remaining()

    def matches(runner, status, plant=False):
        run, start = ("run_id", "experiment_started_at") if plant else ("current_run_id", "started_at")
        if status.get(run) != runner.run_id or not same_start(status.get(start), runner.started_at):
            raise RunnerError("Experiment identity changed; refusing to mutate another lifecycle.")

    def make_runner(name):
        remaining(35)
        runner = runner_factory(replace(config, output=config.output / name), client=http,
            monotonic=monotonic, sleep=pause, now=now)
        runners.append(runner)
        runner.preflight()
        runner.prepare()
        return runner

    def wait_ticks(runner, count):
        observed = set()
        while len(observed) < count:
            remaining(30)
            try:
                runner.observe_once()
            except HttpReadTimeout:
                pause(.2)
                continue
            status = runner.latest_status or {}
            matches(runner, status)
            process = (status.get("integrations") or {}).get("process") or {}
            output = status.get("last_control_output") or {}
            if (status.get("status") == "running" and status.get("active") is True
                    and process.get("sample_valid") is True and process.get("missing_fields") == []
                    and finite(process.get("sample_age_s")) and 0 <= process["sample_age_s"] <= 5
                    and output.get("run_id") == runner.run_id and type(output.get("tick_seq")) is int
                    and (output.get("result") or {}).get("valid") is True and output.get("process_write_error") is None
                    and verified_write({"command": process.get("last_command"), "receipt": process.get("last_write_result"),
                        "write": output.get("process_write")}, run_id=runner.run_id, started_at=runner.started_at)):
                observed.add(output["tick_seq"])
            if len(observed) < count:
                pause(min(1., config.poll_s))
        return capture("verified_ticks", {"identity": runner.identity(), "ticks": sorted(observed), "controller": status})

    def end_controller(runner):
        if runner.run_id is None:
            return None
        if runner.run_id not in end_attempted:
            system = runner.call("central", "GET", "/api/system/status")
            current = system.get("current_experiment") or {}
            if current.get("run_id") != runner.run_id or (runner.started_at and not same_start(current.get("started_at"), runner.started_at)):
                raise RunnerError("Central identity changed; refusing an End for a foreign experiment.")
            end_attempted.add(runner.run_id)
            capture("end_intent", {"identity": runner.identity(), "attempt": 1})
            response = runner.call("central", "POST", f"/api/experiments/{runner.run_id}/finish", {"expected_run_id": runner.run_id})
            capture("end_response", response)
        while True:
            remaining()
            controller = runner.call("controller", "GET", "/api/status")
            experiment = runner.call("central", "GET", f"/api/experiments/{runner.run_id}")
            if runner.started_at:
                matches(runner, controller)
                ready = controller.get("active") is False and controller.get("status") == "stopped" and controller.get("stopped_at")
            else:
                ready = controller.get("active") is False
            if ready and experiment.get("status") in {"completed", "error"}:
                ended[runner.run_id] = capture("end_confirmed", {"identity": runner.identity(), "controller": controller, "experiment": experiment})
                return controller
            pause(.25)

    def inactive_model(runner, controller):
        plant = runner.call("plant", "GET", "/status")
        if runner.started_at:
            matches(runner, plant, plant=True)
        elif plant.get("run_id") not in {None, runner.run_id}:
            raise RunnerError("Foreign model identity prevents cleanup.")
        if plant.get("status") in {"prepared", "running"} or plant.get("running") is True:
            if runner.run_id in stop_attempted:
                raise RunnerError("Owned model stop was already attempted; its unknown mutation is not repeated.")
            stop_attempted.add(runner.run_id)
            capture("normal_model_stop_intent", {"identity": runner.identity(), "attempt": 1})
            plant = runner.call("plant", "POST", "/stop", runner.identity())
            capture("normal_model_stop_response", plant)
        if plant.get("running") is not False or plant.get("status") not in {"stopped", "error", "unprepared"}:
            raise RunnerError("Owned model is not confirmed inactive.")
        proof = {"controller": controller, "plant": plant}
        if runner.started_at:
            inactive_owned(proof, runner.run_id, runner.started_at)
        return capture("dual_inactive", proof)["body"]

    def restore_native(proof):
        nonlocal restored, launched
        if restored is not None:
            return restored
        remaining(25)
        inactive_owned(proof, old.run_id, old.started_at)
        exit_deadline = min(http.deadline - 20, monotonic() + 15)
        while not guard.exited(old_native):
            if monotonic() >= exit_deadline:
                raise RunnerError("The one TERM did not confirm old native exit; no forced kill or duplicate launch.")
            pause(.25)
        capture("native_exit_confirmed", old_native)
        vendor_log = config.output / "vendor-desktop.log"
        # Guard remembers an uncertain spawn and will reject any second launch.
        if not guard.launch_attempted:
            capture("native_launch_intent", {"project": str(project), "attempt": 1, "log_path": str(vendor_log)})
            launched = guard.launch_once(vendor_log, old_native, proof)
            capture("native_launch_response", launched)
        if launched is None:
            raise RunnerError("Desktop launch outcome is unknown; no process may be guessed as our recovery.")
        recovery_deadline = min(http.deadline - 15, monotonic() + 60)
        last_error = None
        while monotonic() < recovery_deadline:
            try:
                process = guard.discover_new(old_native)
                if process["pid"] != launched.get("launcher_pid"):
                    raise RunnerError("Discovered native PID is not the exact desktop process launched by this collector.")
                native = server_probe(native_config, reference)
                guard.verify_same(process)
                if epoch(native["server"]["server_start_time"]) <= epoch(before_native["server"]["server_start_time"]):
                    raise RunnerError("UA ServerStatus.StartTime did not advance with the new native process.")
                restored = capture("native_restored", {"process": process, "native": native})["body"]
                return restored
            except (Exception, KeyboardInterrupt) as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                last_error = f"{type(exc).__name__}: {exc}"
                capture("native_readiness_not_yet_proven", {"reason": last_error, "mutation_retried": False})
                pause(.5)
        raise RunnerError("Native desktop recovery was not proven: " + str(last_error))

    def archive_report(runner):
        if runner.run_id in reported:
            return
        if runner.started_at and runner.run_id in ended:
            runner._validate_completed_archive()  # Original raw-gap rules intentionally unchanged.
            audit = analyze_outage_archive(runner.config.output, runner.run_id, runner.started_at)
            artifacts[runner.run_id] = {"ordinary_archive": deepcopy(runner.archive_validation), "command_audit": audit,
                "scope": "Actual outage/raw gaps and invalid attempts retained; normal archive rules are not relaxed."}
            capture("archive_validation", {"identity": runner.identity(), **artifacts[runner.run_id]})
        runner.write_report()
        reported.add(runner.run_id)
        runner.log.close()

    capture("native_before", {"process": old_native, "native": before_native, "reference_file": reference_file})
    try:
        old = make_runner("fault-run")
        wait_ticks(old, 2)
        current_native = server_probe(native_config, reference)
        guard.verify_same(old_native)
        if not same_start(current_native["server"]["server_start_time"], before_native["server"]["server_start_time"]):
            raise RunnerError("Native server changed before the explicit fault injection.")
        plant_before = old.call("plant", "GET", "/status")
        matches(old, plant_before, plant=True)
        if (plant_before.get("status") != "running" or plant_before.get("running") is not True or plant_before.get("error")
                or not finite(plant_before.get("sample_age_s")) or not 0 <= plant_before["sample_age_s"] <= 5):
            raise RunnerError("The exact model must be healthy and fresh before native outage injection.")
        intent = capture("native_term_intent", {"process": old_native, "identity": old.identity(), "attempt": 1})
        try:
            capture("native_term_response", guard.terminate_once(old_native))
        finally:
            term_attempted = guard.term_attempted
        fault_deadline = min(http.deadline - 60, monotonic() + 30)
        rejected = None
        while monotonic() < fault_deadline:
            accepted_before = set(old.observed_ticks)
            try:
                old.observe_once()
            except HttpReadTimeout as exc:
                capture("fault_status_read_timeout", {"error": str(exc), "mutation_retried": False})
                pause(.2)
                continue
            except RunnerError as exc:
                plant = old.latest_plant or {}
                matches(old, plant, plant=True)
                reason = str(exc)
                expected = reason in {"Test plant stopped or reported an error.",
                    "Test plant heartbeat is stale; repeated old OPC UA values are not new physics."}
                failed_model = (plant.get("running") is False and plant.get("status") == "error" and bool(plant.get("error")))
                stale_model = finite(plant.get("sample_age_s")) and plant["sample_age_s"] > 5
                rejected = capture("actual_runner_rejection", {"reason": reason, "plant": plant, "controller": old.latest_status,
                    "accepted_before": sorted(accepted_before), "accepted_after": sorted(old.observed_ticks),
                    "real_native_model_fault": expected and (failed_model or stale_model)})
                # Always End immediately, including an earlier Controller error.
                # Only the actual model-watchdog rejection qualifies this check.
                controller = end_controller(old)
                if not expected or not (failed_model or stale_model):
                    raise RunnerError("Runner stopped for another actual error; native model-watchdog rejection was not proven.") from exc
                if old.observed_ticks != accepted_before:
                    raise NativeSafetyFailure("Runner accepted a new normal tick during its rejected model observation.")
                proof = inactive_model(old, controller)
                checks["native_model_fault_rejected_and_old_run_ended"] = "PASS"
                break
            capture("legal_inflight_observation", {"controller": old.latest_status, "plant": old.latest_plant,
                "scope": "An actual fresh/in-flight cycle before the watchdog trips is retained, not relabeled as failed."})
            pause(.25)
        else:
            raise NativeSafetyFailure("The real runner did not reject the native-UA model failure within the bounded watchdog observation.")
        restored_native = restore_native(proof)
        checks["same_project_new_native_pid_and_later_ua_start"] = "PASS"
        startup_readiness = deepcopy(old.startup_telegraf_readiness)
        try:
            old._probe_fresh_telegraf(restored_native["native"]["verified_at"])
            readiness = deepcopy(old.startup_telegraf_readiness)
        finally:
            old.startup_telegraf_readiness = startup_readiness
        frozen = verify_frozen_rounds(readiness, reference["node_ids"], restored_native["native"]["verified_at"])
        capture("native_recovery_fresh_telegraf", {"proof": readiness, "validation": frozen,
            "scope": "Fresh reads after native restart with stopped model; not resumption of the old experiment."})
        checks["three_actual_fresh_good_reads_after_native_restart"] = "PASS"
        noresume_deadline = monotonic() + 31
        while monotonic() < noresume_deadline:
            remaining(55)
            controller = old.call("controller", "GET", "/api/status")
            matches(old, controller)
            baseline = ended[old.run_id]["body"]["controller"]
            if (controller.get("status") != "stopped" or controller.get("active") is not False
                    or controller.get("last_control_output") != baseline.get("last_control_output")
                    or (controller.get("scheduler") or {}).get("tick_count") != (baseline.get("scheduler") or {}).get("tick_count")):
                raise NativeSafetyFailure("Old Controller resumed or advanced after UA service recovery.")
            plant = old.call("plant", "GET", "/status")
            inactive_owned({"controller": controller, "plant": plant}, old.run_id, old.started_at)
            if any(plant.get(key) != proof["plant"].get(key) for key in ("sample_seq", "elapsed_s", "heartbeat_at")):
                raise NativeSafetyFailure("Old model advanced its physical clock/samples after its failure and End.")
            capture("no_old_resume", {"controller": controller, "plant": plant})
            pause(1)
        checks["old_run_did_not_resume_for_two_periods"] = "PASS"
        capture("inflight_boundary", {"term_intent_at": intent["observed_at"], "runner_rejection_at": rejected["observed_at"],
            "controller_end_confirmed_at": ended[old.run_id]["observed_at"],
            "last_control_output": ended[old.run_id]["body"]["controller"].get("last_control_output"),
            "scope": "Actual outputs and invalid attempts preserved; no claim of zero in-flight work."})
        archive_report(old)
        remaining(45)
        fixture = recovery_fixture(old, config.output / "recovery-fixture.json")
        capture("guarded_recovery_initialization", fixture)
        if fixture.get("action_status") != "PASS":
            raise RunnerError("Inactive guarded 42 C recovery initialization did not pass.")
        recovery = make_runner("recovery-run")
        if recovery.run_id == old.run_id or epoch(recovery.started_at) <= epoch(old.started_at):
            raise NativeSafetyFailure("Recovery identity must be distinct and later; old experiment cannot resume.")
        wait_ticks(recovery, 1)
        checks["distinct_new_experiment_verified_write"] = "PASS"
        inactive_model(recovery, end_controller(recovery))
        archive_report(recovery)
    except (Exception, KeyboardInterrupt) as exc:
        failure(exc, "capture")
    finally:
        http.deadline = final_deadline
        old_proof = None
        for runner in reversed(runners):
            if runner.log._stream.closed:
                continue
            try:
                if runner.run_id is not None:
                    controller = end_controller(runner)
                    proof = inactive_model(runner, controller)
                    if runner is old:
                        old_proof = proof
                if not runner.log._stream.closed:
                    archive_report(runner)
            except (Exception, KeyboardInterrupt) as exc:
                failure(exc, "owned_cleanup")
                try:
                    if runner.run_id not in reported:
                        runner.write_report()
                        reported.add(runner.run_id)
                except Exception as report_error:
                    failure(report_error, "ordinary_report")
                runner.log.close()
        # Restore the native desktop after a partial capture, but only following
        # exact old-run inactivity. Never retry TERM or an uncertain launcher.
        if term_attempted and restored is None and old_proof is not None:
            try:
                restore_native(old_proof)
            except (Exception, KeyboardInterrupt) as exc:
                failure(exc, "native_cleanup_recovery")
        try:
            after_sources = source_provider()
        except Exception as exc:
            after_sources = {"status": "UNAVAILABLE", "error": str(exc)}
            failure(exc, "source_fingerprint")
        try:
            host_after = {str(path.relative_to(root)): file_hash(path) for path in host_paths}
            project_after = file_hash(Path(project))
        except OSError as exc:
            host_after, project_after = {}, None
            failure(exc, "artifact_fingerprint")
        elapsed = monotonic() - began
        if elapsed > total_timeout_s:
            errors.append({"phase": "budget", "message": "Complete native-outage fixture exceeded its declared real-time budget."})
        if (after_sources.get("source_sha256") != sources.get("source_sha256") or host_before != host_after
                or project_after != old_native["project_sha256"]):
            errors.append({"phase": "immutability", "message": "Production source/config, host harness or native baseline changed during capture."})
        required = {"native_model_fault_rejected_and_old_run_ended", "same_project_new_native_pid_and_later_ua_start",
            "three_actual_fresh_good_reads_after_native_restart", "old_run_did_not_resume_for_two_periods", "distinct_new_experiment_verified_write"}
        safety = "FAIL" if any(item.get("unsafe_observation") for item in errors) else "PASS" if required <= checks.keys() and not errors else "INCOMPLETE"
        artifact_status = "PASS" if len(artifacts) == 2 and all(
            item["ordinary_archive"].get("status") == "PASS" and item["command_audit"].get("status") == "PASS" for item in artifacts.values()) else "FAIL"
        evidence = {"case": "native_prosys_disconnect", "scope": "Real native-UA process outage and supervised End/new-run recovery; not AMQP failure, hardware or paper acceptance.",
            "budget_s": total_timeout_s, "elapsed_s": elapsed, "captures": captures, "errors": errors, "checks": checks,
            "artifacts": artifacts, "source_manifest": sources, "source_manifest_at_end": after_sources,
            "host_harness_hashes": host_before, "host_harness_hashes_at_end": host_after,
            "project_sha256": old_native["project_sha256"], "project_sha256_at_end": project_after,
            "native_term_attempted": term_attempted, "native_launch_attempted": guard.launch_attempted,
            "end_attempted_run_ids": sorted(end_attempted), "model_stop_attempted_run_ids": sorted(stop_attempted), "completed_at": now()}
        result = {"case": evidence["case"], "status": "FAIL" if safety == "FAIL" else "PASS" if safety == artifact_status == "PASS" else "INCOMPLETE",
            "safety_status": safety, "artifact_status": artifact_status, "scope": evidence["scope"], "checks": checks, "errors": errors,
            "artifacts": artifacts, "elapsed_s": elapsed, "budget_s": total_timeout_s, "fault_evidence": "fault_evidence.json"}
        save_exclusive(config.output / "fault_evidence.json", evidence)
        save_exclusive(config.output / "fault_result.json", result)
        log.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disconnect", action="store_true")
    parser.add_argument("--native-pid", type=int, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    args = parser.parse_args(argv)
    if not args.execute_disconnect:
        parser.error("--execute-disconnect is required before any native/experiment action")
    result = run_native_disconnect_check(RunnerConfig(output=args.output, mode="short-dynamic",
        commissioning_evidence=args.commissioning_evidence, telemetry_config=args.telemetry_config),
        native_pid=args.native_pid, project=args.project, execute_disconnect=True)
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
