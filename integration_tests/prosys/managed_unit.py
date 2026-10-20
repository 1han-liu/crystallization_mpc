"""Detached, explicitly authorized user-unit launcher for TWO managed repo tools.

No action on import.  ``plan`` only hashes files and writes a NEW plan. ``launch``
and ``stop`` require distinct authorization flags. Tests inject fake subprocesses;
the real no-network signal probe/unit test is a separate, NOT-YET-RUN operation.
No linger, power policy, container, native-server or arbitrary-service control.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import stat
import subprocess
import time
import uuid


PREFIX = "mpcrystal-managed-"
TOKEN = re.compile(r"^[A-Za-z0-9]{12,64}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
INVOCATION = re.compile(r"^[0-9a-f]{32}$")
TOOLS = {"managed_paper_run.py", "managed_signal_probe.py"}
BINARIES = {"run": "/usr/bin/systemd-run", "ctl": "/usr/bin/systemctl",
            "inhibit": "/usr/bin/systemd-inhibit", "timeout": "/usr/bin/timeout"}
PROPERTIES = ("Id", "LoadState", "Transient", "Description", "InvocationID", "Type",
              "Restart", "KillMode", "KillSignal", "RuntimeMaxUSec", "TimeoutStopUSec",
              "ActiveState", "SubState", "Result", "MainPID", "ExecMainPID",
              "ExecMainCode", "ExecMainStatus", "ExecMainStartTimestampMonotonic",
              "ExecMainExitTimestampMonotonic", "ControlPID")


class UnitError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise UnitError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def json_bytes(document):
    return (json.dumps(document, indent=2, sort_keys=True, allow_nan=False)+"\n").encode()


def exclusive(path, document):
    data = json_bytes(document)
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(data).hexdigest()


def pinned(path, expected):
    need(isinstance(expected, str) and SHA.fullmatch(expected), "Explicit full SHA256 required.")
    with Path(path).open("rb") as stream:
        data = stream.read(4*1024*1024+1)
    need(len(data) <= 4*1024*1024 and hashlib.sha256(data).hexdigest() == expected,
         "Manifest/receipt bytes differ from the externally fixed SHA256.")
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "Duplicate JSON key.")
            result[key] = value
        return result
    doc = json.loads(data, object_pairs_hook=pairs)
    json.dumps(doc, allow_nan=False)
    return doc


@dataclass(frozen=True)
class Budgets:
    startup_s: int = 180
    active_s: int = 21600
    cleanup_s: int = 300
    report_s: int = 60
    finalize_s: int = 30

    def __post_init__(self):
        need(all(type(v) is int and v > 0 for v in asdict(self).values()), "Positive integer budgets required.")
        need(self.active_s <= 21600, "Active cap must not exceed six hours; no launcher extension.")
        # Base defaults alone allow 45s stop + 90s archive + five 5s downloads.
        # Annotation/Influx is covered by the managed wrapper's GLOBAL cleanup
        # deadline, not falsely assumed to use the base HTTP client's 5s bound.
        need(self.cleanup_s > 45+90+25 and self.report_s >= 60,
             "Cleanup/report budgets do not cover declared stop/archive/download/report work.")
        # Match the managed wrapper's accepted limits before any unit launch.
        need(self.startup_s <= 180 and self.cleanup_s <= 300 and self.report_s <= 60
             and 30 <= self.finalize_s <= 60,
             "Launcher overhead budgets must match the managed wrapper's limits.")

    @property
    def runtime_s(self):
        return self.startup_s+self.active_s+self.cleanup_s+self.report_s+self.finalize_s

    @property
    def stop_s(self):
        return self.cleanup_s+self.report_s+self.finalize_s


def identity(token, uid):
    need(isinstance(token, str) and TOKEN.fullmatch(token), "Unit token must be 12-64 alphanumeric characters.")
    need(type(uid) is int and uid >= 0, "Invalid owner UID.")
    return PREFIX+token+".service", f"MPCrystal managed host owner={uid}:{token}"


def _absolute(path, cwd):
    need(isinstance(path, str) and path and "\x00" not in path and "\n" not in path,
         "Command path must be nonempty, without control characters.")
    return posixpath.normpath(path if path.startswith("/") else str(cwd)+"/"+path)


def _paper_hours(command):
    name = "--max-hours"
    need(sum(v == name or v.startswith(name+"=") for v in command[3:]) <= 1,
         "Duplicate active duration flag.")
    if name in command[3:]:
        index = command.index(name, 3)
        need(index+1 < len(command), "Missing active duration value.")
        value = command[index+1]
    else:
        value = next((v.split("=", 1)[1] for v in command[3:] if v.startswith(name+"=")), "6")
    try:
        hours = float(value)
    except (TypeError, ValueError) as exc:
        raise UnitError("Invalid active duration value.") from exc
    need(math.isfinite(hours) and 0 < hours <= 6,
         "Managed active duration must be positive and at most six hours.")
    return hours


def _command_budgets(command, budgets):
    if budgets is not None:
        return budgets
    if (isinstance(command, (list, tuple)) and len(command) >= 3
            and all(isinstance(v, str) for v in command)
            and Path(command[2]).name == "managed_paper_run.py"):
        seconds = _paper_hours(command)*3600
        need(seconds.is_integer(), "Active duration must resolve to whole seconds; no implicit rounding.")
        return Budgets(active_s=int(seconds))
    return Budgets()  # Signal probes keep their existing, separate budget policy.


def normalize_command(command, cwd, budgets, *, _legacy_budget_binding=False):
    need(isinstance(command, (list, tuple)) and len(command) >= 3
         and all(isinstance(v, str) and v and not any(c in v for c in "\x00\n\r") for v in command),
         "Command must be an explicit argv array, never shell text.")
    command = list(command)
    need(command[1] == "-B", "Managed Python command must explicitly use -B; no -c/-m/shell launcher.")
    command[0], command[2] = _absolute(command[0], cwd), _absolute(command[2], cwd)
    script = Path(command[2])
    need(script.parent == Path(cwd)/"integration_tests/prosys" and script.name in TOOLS,
         "Only managed_paper_run.py or managed_signal_probe.py in this repo is launchable.")
    if script.name == "managed_signal_probe.py":
        need("--probe" in command[3:], "Synthetic probe requires its explicit --probe authorization.")
        need(budgets.active_s == 21600, "Signal probe keeps its existing separate six-hour outer budget.")
    else:
        need("--run-paper" in command[3:], "Paper wrapper requires its explicit --run-paper authorization.")
        hours = _paper_hours(command)
        if not _legacy_budget_binding:
            need(hours*3600 == budgets.active_s, "Inner/outer active budget mismatch.")
        if not any(v == "--max-hours" or v.startswith("--max-hours=") for v in command[3:]):
            command.extend(["--max-hours", "6"])
        for name, value in (("--startup-timeout-seconds", budgets.startup_s),
                            ("--cleanup-timeout-seconds", budgets.cleanup_s),
                            ("--report-timeout-seconds", budgets.report_s)):
            need(sum(v == name or v.startswith(name+"=") for v in command[3:]) <= 1,
                 "Duplicate managed budget flag.")
            if name in command:
                index = command.index(name)
                need(index+1 < len(command) and command[index+1] == str(value), "Inner/outer budget mismatch.")
            elif any(v.startswith(name+"=") for v in command):
                need(name+"="+str(value) in command, "Inner/outer budget mismatch.")
            else:
                command.extend([name, str(value)])
    return command


def exit_capture_spec(command, cwd, token, uid, budgets, bins):
    """Pure, deterministic owned path/guard/ExecStopPost binding; no writes."""
    unit, _ = identity(token, uid)
    directory = Path(cwd)/".runtime/prosys-test/reports"/("managed-unit-exit-"+token)
    guard = {"schema_version": 1, "kind": "mpcrystal_managed_unit_exit_guard",
             "unit_token": token, "unit_name": unit, "uid": uid, "working_directory": str(cwd),
             "output_directory": str(directory), "output_file": str(directory/"exit.json")}
    guard_sha = hashlib.sha256(json_bytes(guard)).hexdigest()
    collector = str(Path(cwd)/"integration_tests/prosys/managed_unit_exit.py")
    # External timeout leaves a 3-second margin within finalization grace.
    argv = [bins["timeout"], "--signal=TERM", "--kill-after=2s", str(budgets.finalize_s-5)+"s",
            command[0], "-B", collector, "--guard", str(directory/"guard.json"),
            "--guard-sha256", guard_sha, "--unit", unit]
    return {"directory": str(directory), "guard_path": str(directory/"guard.json"),
            "output_path": str(directory/"exit.json"), "guard": guard, "guard_file_sha256": guard_sha,
            "collector_path": collector, "argv": argv, "argv_sha256": digest(argv),
            "timeout_s": budgets.finalize_s-5, "kill_after_s": 2,
            "scope": "Manager MAIN/inhibitor exit fields only; never the Python child or a success verdict."}


def systemd_exec(argv):
    """Quote systemd exec syntax, not a shell; ':' disables dollar expansion."""
    need(all(isinstance(value, str) and value and not any(c in value for c in "\x00\n\r") for value in argv),
         "Invalid ExecStopPost argv.")
    return ":"+" ".join('"'+value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")+'"'
                        for value in argv)


def build_unit_argv(command, *, unit_token, working_directory, budgets=None, uid=None, binaries=None,
                    _legacy_budget_binding=False):
    """Pure argv construction: no files, subprocess, systemd or network calls."""
    cwd = Path(working_directory)
    need(cwd.is_absolute() and ".." not in cwd.parts, "Absolute normalized working directory required.")
    owner = os.getuid() if uid is None else uid
    unit, description = identity(unit_token, owner)
    bins = dict(BINARIES if binaries is None else binaries)
    need(set(bins) == set(BINARIES) and all(isinstance(v, str) and Path(v).is_absolute() for v in bins.values()),
         "Explicit absolute systemd binary paths required.")
    budgets = _command_budgets(command, budgets)
    actual = normalize_command(command, cwd, budgets, _legacy_budget_binding=_legacy_budget_binding)
    capture = exit_capture_spec(actual, cwd, unit_token, owner, budgets, bins)
    properties = {"Type": "exec", "Restart": "no", "KillMode": "control-group",
                  "KillSignal": "SIGTERM", "FinalKillSignal": "SIGKILL", "SendSIGKILL": "yes",
                  "RuntimeMaxSec": str(budgets.runtime_s)+"s", "TimeoutStopSec": str(budgets.stop_s)+"s",
                  "WorkingDirectory": str(cwd), "Description": description,
                  "StandardOutput": "journal", "StandardError": "journal",
                  "ExecStopPost": systemd_exec(capture["argv"])}
    return [bins["run"], "--user", "--unit="+unit, "--expand-environment=no",
            *("--property="+k+"="+v for k, v in properties.items()), "--", bins["inhibit"],
            "--what=sleep:idle", "--mode=block", "--who="+unit,
            "--why=Bounded MPCrystal host test; power protections unchanged", "--", *actual]


def pid_metadata():
    return {"pid": os.getpid(), "ppid": os.getppid(), "sid": os.getsid(0),
            "pgid": os.getpgid(0), "uid": os.getuid(), "monotonic_ns": time.monotonic_ns(),
            "observed_at": datetime.now(timezone.utc).isoformat()}


def _file_binding(path):
    path = Path(path)
    need(path.is_file(), "Command file is absent: "+str(path))
    return {"path": str(path), "resolved_path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def make_plan(command, *, working_directory, budgets=None, unit_token=None, binaries=None):
    cwd = Path(working_directory).resolve()
    token = uuid.uuid4().hex if unit_token is None else unit_token
    unit, description = identity(token, os.getuid())
    bins = dict(BINARIES if binaries is None else binaries)
    budgets = _command_budgets(command, budgets)
    actual = normalize_command(command, cwd, budgets)
    need(Path(actual[2]).resolve().parent == cwd/"integration_tests/prosys", "Repo tool symlink escapes tool directory.")
    argv = build_unit_argv(actual, unit_token=token, working_directory=cwd, budgets=budgets, binaries=bins)
    capture = exit_capture_spec(actual, cwd, token, os.getuid(), budgets, bins)
    file_paths = {**bins, "python": actual[0], "wrapper": actual[2], "launcher": str(Path(__file__).resolve())}
    file_paths["exit_collector"] = capture["collector_path"]
    if Path(actual[2]).name == "managed_signal_probe.py":
        file_paths["support_latch"] = str(Path(actual[2]).with_name("managed_paper_run.py"))
    files = {name: _file_binding(path) for name, path in file_paths.items()}
    return {"schema_version": 3, "kind": "mpcrystal_managed_unit_plan", "execution": "NOT_LAUNCHED",
            "unit_token": token, "unit_name": unit, "uid": os.getuid(), "description": description,
            "working_directory": str(cwd), "binaries": bins, "command": actual,
            "command_sha256": digest(actual), "launch_argv": argv, "launch_argv_sha256": digest(argv),
            "command_files": files, "budgets": asdict(budgets), "runtime_max_s": budgets.runtime_s,
            "timeout_stop_s": budgets.stop_s, "exit_capture": capture,
            "cleanup_scope": "Declared GLOBAL cleanup includes annotation/Influx; separate report deadline, then bounded finalization grace.",
            "deadline_limit": "Not a completion guarantee: signal reaction, blocking/uninterruptible I/O or SIGKILL can leave cleanup/reports incomplete.",
            "creator": pid_metadata(), "power_linger_changes": "NONE",
            "scope": "Host wrapper ownership only; no experiment/control/hardware verdict."}


def validate_plan(plan):
    need(isinstance(plan, dict) and plan.get("schema_version") in {2, 3}
         and plan.get("kind") == "mpcrystal_managed_unit_plan" and plan.get("execution") == "NOT_LAUNCHED",
         "Not a managed unit plan.")
    need(plan["uid"] == os.getuid(), "Manifest belongs to another user manager.")
    unit, description = identity(plan["unit_token"], plan["uid"])
    budgets = Budgets(**plan["budgets"])
    legacy = plan["schema_version"] == 2
    need(not legacy or budgets.active_s == 21600,
         "Legacy plan retains its recorded six-hour outer cap; no silent budget upgrade.")
    expected = build_unit_argv(plan["command"], unit_token=plan["unit_token"],
                               working_directory=plan["working_directory"], budgets=budgets,
                               uid=plan["uid"], binaries=plan["binaries"], _legacy_budget_binding=legacy)
    capture = exit_capture_spec(plan["command"], plan["working_directory"], plan["unit_token"],
                                plan["uid"], budgets, plan["binaries"])
    need(plan["unit_name"] == unit and plan["description"] == description
         and plan["launch_argv"] == expected and plan["launch_argv_sha256"] == digest(expected)
         and plan["command_sha256"] == digest(plan["command"])
         and plan["runtime_max_s"] == budgets.runtime_s and plan["timeout_stop_s"] == budgets.stop_s
         and plan["exit_capture"] == capture,
         "Plan identity/argv/budget binding differs.")


class SubprocessBackend:
    """Only explicit CLI launch/status/stop uses the real subprocess backend."""
    def __call__(self, argv, *, timeout):
        return subprocess.run(argv, shell=False, capture_output=True, text=True, check=False, timeout=timeout)


def _show(plan, backend):
    argv = [plan["binaries"]["ctl"], "--user", "show", "--no-pager",
            "--property="+",".join(PROPERTIES), "--", plan["unit_name"]]
    response = backend(argv, timeout=10)
    need(len(response.stdout) <= 256*1024 and len(response.stderr) <= 256*1024, "Oversized systemctl output.")
    values = {}
    for line in response.stdout.splitlines():
        need("=" in line, "Malformed systemctl show output.")
        key, value = line.split("=", 1)
        need(key in PROPERTIES and key not in values, "Unexpected/duplicate systemctl property.")
        values[key] = value
    absent = values.get("LoadState") == "not-found"
    need(response.returncode == 0 or absent and response.returncode in {1, 4}, "systemctl show failed.")
    need(values.get("Id") in {None, "", plan["unit_name"]} if absent
         else values.get("Id") == plan["unit_name"], "systemctl returned another unit identity.")
    return {"properties": values, "returncode": response.returncode,
            "stderr": response.stderr, "observer": pid_metadata()}


def _owned(plan, snapshot, invocation_id=None):
    values = snapshot["properties"]
    need(values.get("Id") == plan["unit_name"] and values.get("LoadState") == "loaded"
         and values.get("Transient") == "yes" and values.get("Description") == plan["description"]
         and INVOCATION.fullmatch(values.get("InvocationID", "")), "Live unit ownership is not proven.")
    if invocation_id is not None:
        need(values["InvocationID"] == invocation_id, "Unit invocation changed; refusing to manage a replacement.")


def launch(plan, *, intent_path, result_path, backend, execute_launch=False):
    need(execute_launch is True, "Explicit execute_launch required; no systemd action occurred.")
    validate_plan(plan)
    need(plan["schema_version"] == 3,
         "Legacy plan is management-only; generate a new exact-budget plan before launch.")
    need(Path(intent_path) != Path(result_path) and not Path(intent_path).exists()
         and not Path(result_path).exists(), "Intent/result must be distinct NEW paths.")
    directory = Path(plan["exit_capture"]["directory"])
    need(all(Path(path).resolve() != directory and directory not in Path(path).resolve().parents
             for path in (intent_path, result_path)), "Launch artifacts may not overlap the reserved exit collector directory.")
    expected_files = {**plan["binaries"], "python": plan["command"][0],
                      "wrapper": plan["command"][2], "launcher": str(Path(__file__).resolve()),
                      "exit_collector": plan["exit_capture"]["collector_path"]}
    if Path(plan["command"][2]).name == "managed_signal_probe.py":
        expected_files["support_latch"] = str(Path(plan["command"][2]).with_name("managed_paper_run.py"))
    need(set(plan["command_files"]) == set(expected_files), "Command file binding set differs.")
    for key, path in expected_files.items():
        need(plan["command_files"][key] == _file_binding(path), "Command file changed before launch: "+key)
    absent = _show(plan, backend)
    need(absent["properties"].get("LoadState") == "not-found", "Unique unit already exists; no launch or stop attempted.")
    capture = plan["exit_capture"]
    need(directory == directory.resolve() and not directory.exists(), "Exit evidence directory must be canonical and NEW.")
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    guard_sha = exclusive(capture["guard_path"], capture["guard"])
    os.chmod(capture["guard_path"], 0o600)
    need(guard_sha == capture["guard_file_sha256"], "Written exit guard differs from its bound bytes.")
    intent = {"schema_version": 1, "kind": "mpcrystal_owned_unit_launch_intent", "plan": plan,
              "plan_sha256": digest(plan), "prelaunch_absence": absent, "execution_authorized": True,
              "exit_guard_file_sha256": guard_sha, "launcher": pid_metadata()}
    intent_sha = exclusive(intent_path, intent)  # Durable intent before any mutation.
    result = {"schema_version": 1, "kind": "mpcrystal_owned_unit_launch_result",
              "intent_file_sha256": intent_sha, "plan_sha256": digest(plan),
              "unit_name": plan["unit_name"], "launch_attempted": True,
              "launch_status": "OUTCOME_UNCONFIRMED", "invocation_id": None}
    try:
        response = backend(plan["launch_argv"], timeout=plan["budgets"]["startup_s"])
        result["systemd_run"] = {"returncode": response.returncode,
                                 "stdout": response.stdout, "stderr": response.stderr}
        observed = _show(plan, backend)
        result["snapshot"] = observed
        if observed["properties"].get("LoadState") != "not-found":
            _owned(plan, observed)
            result["invocation_id"] = observed["properties"]["InvocationID"]
            result["launch_status"] = ("OWNED_UNIT_OBSERVED" if response.returncode == 0
                                       else "OWNED_UNIT_OBSERVED_AFTER_LAUNCHER_ERROR")
        # Even nonzero systemd-run may have created a unit. Never retry it.
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "detail": str(exc)}
    result["result_file_sha256_scope"] = "Hash the exclusive result file externally before management."
    exclusive(result_path, result)
    return result


def validate_ownership_documents(intent, result, *, intent_sha256):
    need(intent.get("kind") == "mpcrystal_owned_unit_launch_intent"
         and intent.get("schema_version") == 1 and intent.get("execution_authorized") is True,
         "Not an owned launch intent.")
    plan = intent["plan"]
    validate_plan(plan)
    need(intent.get("plan_sha256") == digest(plan)
         and intent.get("exit_guard_file_sha256") == plan["exit_capture"]["guard_file_sha256"]
         and intent["prelaunch_absence"]["properties"].get("LoadState") == "not-found"
         and result.get("kind") == "mpcrystal_owned_unit_launch_result"
         and result.get("schema_version") == 1 and result.get("intent_file_sha256") == intent_sha256
         and result.get("plan_sha256") == digest(plan) and result.get("unit_name") == plan["unit_name"]
         and result.get("launch_attempted") is True
         and isinstance(result.get("invocation_id"), str) and INVOCATION.fullmatch(result["invocation_id"]),
         "Launch receipt has no exact original owned invocation; investigate uncertain launch manually.")
    return plan


def valid_utc_marker(value):
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def persisted_exit(plan, invocation_id):
    capture = plan["exit_capture"]
    path = Path(capture["output_path"])
    if not path.exists():
        return {"status": "NOT_AVAILABLE", "path": str(path)}
    directory, guard_path = path.parent, Path(capture["guard_path"])
    for target, directory_required in ((directory, True), (guard_path, False), (path, False)):
        details = target.lstat()
        need((stat.S_ISDIR(details.st_mode) if directory_required else stat.S_ISREG(details.st_mode))
             and not target.is_symlink() and target == target.resolve() and details.st_uid == plan["uid"]
             and stat.S_IMODE(details.st_mode) & 0o077 == 0, "Persisted exit paths are not exact private owned files.")
    with guard_path.open("rb") as stream:
        guard_data = stream.read(65537)
    need(len(guard_data) <= 65536 and hashlib.sha256(guard_data).hexdigest() == capture["guard_file_sha256"],
         "Exit guard changed.")
    with path.open("rb") as stream:
        data = stream.read(4*1024*1024+1)
    need(len(data) <= 4*1024*1024, "Oversized persisted exit capture.")
    sha = hashlib.sha256(data).hexdigest()
    document = pinned(path, sha)
    fields = document.get("manager_exit_fields", {})
    metadata = document.get("collector", {})
    need(document.get("schema_version") == 1 and document.get("kind") == "mpcrystal_managed_unit_exit_capture"
         and document.get("unit_name") == plan["unit_name"]
         and document.get("guard_file_sha256") == capture["guard_file_sha256"]
         and valid_utc_marker(document.get("recorded_at"))
         and set(fields) == {"EXIT_CODE", "EXIT_STATUS", "SERVICE_RESULT", "INVOCATION_ID"}
         and fields.get("INVOCATION_ID") == invocation_id
         and metadata.get("uid") == plan["uid"] and any(
             plan["unit_name"] in line.split(":", 2)[-1].split("/") for line in metadata.get("cgroup", [])),
         "Persisted exit does not bind the original owned invocation.")
    complete = all(isinstance(value, str) and value and len(value) <= 128
                   and not any(c in value for c in "\x00\n\r") for value in fields.values())
    need(document.get("capture_status") == ("MANAGER_EXIT_FIELDS_CAPTURED" if complete else "MANAGER_EXIT_FIELDS_INCOMPLETE"),
         "Persisted exit completeness claim differs from its fields.")
    return {"status": document["capture_status"], "path": str(path), "file_sha256": sha, "document": document}


def capture_status(intent, result, *, intent_sha256, backend):
    plan = validate_ownership_documents(intent, result, intent_sha256=intent_sha256)
    observed = _show(plan, backend)
    values = observed["properties"]
    saved = persisted_exit(plan, result["invocation_id"])
    if values.get("LoadState") == "not-found":
        state = ("MAIN_EXIT_OBSERVED_STOP_POST" if saved["status"] == "MANAGER_EXIT_FIELDS_CAPTURED"
                 else "TERMINAL_STATUS_UNAVAILABLE_UNIT_NOT_FOUND")
    else:
        _owned(plan, observed, result["invocation_id"])
        state = ("TERMINAL_OBSERVED" if values.get("ActiveState") in {"inactive", "failed"}
                 and values.get("MainPID") == "0" else "RUNNING_OR_STOPPING")
    return {"schema_version": 1, "kind": "mpcrystal_owned_unit_external_status",
            "unit_name": plan["unit_name"], "invocation_id": result["invocation_id"],
            "intent_file_sha256": intent_sha256, "external_state": state, "snapshot": observed,
            "persisted_manager_exit": saved,
            "scope": "Raw supervisor status only; StopPost observes MAIN exit, not final whole-unit result. Exit143/timeout/nonzero are NOT success or experiment verdicts."}


def stop(intent, result, *, intent_sha256, backend, execute_stop=False):
    need(execute_stop is True, "Explicit execute_stop required; no stop action occurred.")
    plan = validate_ownership_documents(intent, result, intent_sha256=intent_sha256)
    observed = _show(plan, backend)
    _owned(plan, observed, result["invocation_id"])
    need(observed["properties"].get("ActiveState") in {"active", "activating", "deactivating"},
         "Unit is not running; no repeated stop or alternate target attempted.")
    response = backend([plan["binaries"]["ctl"], "--user", "stop", "--no-block", "--", plan["unit_name"]], timeout=10)
    return {"schema_version": 1, "kind": "mpcrystal_owned_unit_stop_request",
            "unit_name": plan["unit_name"], "invocation_id": result["invocation_id"],
            "intent_file_sha256": intent_sha256, "before": observed,
            "returncode": response.returncode, "stdout": response.stdout, "stderr": response.stderr,
            "completion": "NOT_ASSERTED_CAPTURE_TERMINAL_STATUS_SEPARATELY"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--command-json", type=Path, required=True)
    plan_parser.add_argument("--working-directory", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    for action in ("launch", "status", "stop"):
        p = commands.add_parser(action)
        p.add_argument("--manifest", type=Path, required=True)
        p.add_argument("--manifest-sha256", required=True)
        p.add_argument("--output", type=Path, required=True)
        if action == "launch":
            p.add_argument("--intent-output", type=Path, required=True)
            p.add_argument("--execute-launch", action="store_true")
        else:
            p.add_argument("--launch-result", type=Path, required=True)
            p.add_argument("--launch-result-sha256", required=True)
        if action == "stop":
            p.add_argument("--execute-stop", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "launch" and not args.execute_launch or args.action == "stop" and not args.execute_stop:
        parser.error("Explicit execution flag required; no unit action occurred.")
    need(not args.output.exists(), "Output already exists; no action occurred.")
    if args.action == "plan":
        plan = make_plan(json.loads(args.command_json.read_text()), working_directory=args.working_directory)
        exclusive(args.output, plan)
        return 0
    doc = pinned(args.manifest, args.manifest_sha256)
    backend = SubprocessBackend()
    if args.action == "launch":
        result = launch(doc, intent_path=args.intent_output, result_path=args.output,
                        backend=backend, execute_launch=args.execute_launch)
        return 0 if result["launch_status"] == "OWNED_UNIT_OBSERVED" else 2
    receipt = pinned(args.launch_result, args.launch_result_sha256)
    if args.action == "status":
        result = capture_status(doc, receipt, intent_sha256=args.manifest_sha256, backend=backend)
    else:
        result = stop(doc, receipt, intent_sha256=args.manifest_sha256,
                      backend=backend, execute_stop=args.execute_stop)
    exclusive(args.output, result)
    return 0 if args.action == "status" or result["returncode"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
