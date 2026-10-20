"""ExecStopPost-only persistence of the unit manager's MAIN-process exit fields.

No action on import. No application, network, subprocess, or service calls.
The inhibitor is the unit main process: these fields are NOT the Python child's
exit status, a cleanup-success verdict, or a Prosys/experiment verdict. The
manager invokes this tool under a separately bound external timeout. Missing or
unwritable evidence stays unknown; there is no retry or overwrite.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


EXIT_KEYS = ("EXIT_CODE", "EXIT_STATUS", "SERVICE_RESULT", "INVOCATION_ID")
PREFIX = "mpcrystal-managed-"
TOKEN = re.compile(r"^[A-Za-z0-9]{12,64}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
INVOCATION = re.compile(r"^[0-9a-f]{32}$")


class ExitCaptureError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise ExitCaptureError(message)


def _deny_external_actions(event, args):
    if event in {"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                 "subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn",
                 "os.exec", "os.kill", "os.killpg"}:
        raise RuntimeError("The exit collector forbids network/process/signal actions.")


def _strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "Duplicate guard JSON key.")
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=pairs)
    json.dumps(value, allow_nan=False)
    need(isinstance(value, dict), "Guard must be a JSON object.")
    return value


def _private(path, *, directory):
    details = path.lstat()
    need((stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode))
         and not path.is_symlink() and details.st_uid == os.getuid()
         and stat.S_IMODE(details.st_mode) & 0o077 == 0,
         "Evidence directory/guard must be private, nonsymlink, and owned by this UID.")


def load_guard(path, expected_sha256, expected_unit):
    path = Path(path)
    need(path.is_absolute() and path.name == "guard.json" and path == path.resolve(),
         "Explicit canonical guard.json path required.")
    need(isinstance(expected_sha256, str) and SHA.fullmatch(expected_sha256), "Full guard SHA256 required.")
    _private(path.parent, directory=True)
    _private(path, directory=False)
    with path.open("rb") as stream:
        data = stream.read(65537)
    need(len(data) <= 65536 and hashlib.sha256(data).hexdigest() == expected_sha256,
         "Guard bytes differ from the bound SHA256.")
    guard = _strict_json(data)
    token = guard.get("unit_token")
    need(isinstance(token, str) and TOKEN.fullmatch(token), "Invalid owned token.")
    unit = PREFIX+token+".service"
    need(guard.get("schema_version") == 1 and guard.get("kind") == "mpcrystal_managed_unit_exit_guard"
         and type(guard.get("uid")) is int and guard["uid"] == os.getuid()
         and guard.get("unit_name") == unit == expected_unit, "Guard ownership/unit differs.")
    cwd = Path(guard.get("working_directory", ""))
    expected_directory = cwd/".runtime/prosys-test/reports"/("managed-unit-exit-"+token)
    need(cwd.is_absolute() and cwd == cwd.resolve() and path.parent == expected_directory
         and guard.get("output_directory") == str(expected_directory)
         and guard.get("output_file") == str(expected_directory/"exit.json"),
         "Guard output must be the exact new owned unit evidence directory.")
    return guard


def self_metadata():
    return {"pid": os.getpid(), "ppid": os.getppid(), "uid": os.getuid(),
            "cgroup": Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()}


def capture(guard_path, guard_sha256, unit, *, environment, metadata_factory=self_metadata):
    """Environment injection is for fake tests; CLI only reads the FOUR keys."""
    guard = load_guard(guard_path, guard_sha256, unit)
    metadata = metadata_factory()
    need(metadata.get("uid") == guard["uid"] and any(
        unit in line.split(":", 2)[-1].split("/") for line in metadata.get("cgroup", [])),
        "Collector is not in the exact owned unit cgroup.")
    # Never enumerate or serialize the process environment (credentials may exist).
    fields = {key: environment.get(key) for key in EXIT_KEYS}
    complete = all(isinstance(value, str) and value and len(value) <= 128
                   and not any(c in value for c in "\x00\n\r") for value in fields.values())
    complete = complete and bool(INVOCATION.fullmatch(fields.get("INVOCATION_ID") or ""))
    document = {"schema_version": 1, "kind": "mpcrystal_managed_unit_exit_capture",
                "unit_name": unit, "guard_file_sha256": guard_sha256,
                "manager_exit_fields": fields, "collector": metadata,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "capture_status": "MANAGER_EXIT_FIELDS_CAPTURED" if complete else "MANAGER_EXIT_FIELDS_INCOMPLETE",
                "service_result_scope": "Snapshot supplied to ExecStopPost; later collector/StopPost failure may change the final unit result.",
                "scope": "systemd unit MAIN process (systemd-inhibit), NOT the Python child; no success/cleanup/Prosys verdict."}
    descriptor = os.open(guard["output_file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guard", type=Path, required=True)
    parser.add_argument("--guard-sha256", required=True)
    parser.add_argument("--unit", required=True)
    args = parser.parse_args(argv)
    sys.addaudithook(_deny_external_actions)
    document = capture(args.guard, args.guard_sha256, args.unit,
                       environment={key: os.environ.get(key) for key in EXIT_KEYS})
    return 0 if document["capture_status"] == "MANAGER_EXIT_FIELDS_CAPTURED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
