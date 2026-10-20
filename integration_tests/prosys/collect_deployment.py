"""Collect fresh, read-only identity/source evidence for five isolated app services.

Only Podman inspect and Python byte hashing are executed. No lifecycle, HTTP,
OPC UA, or control operations occur. Reports expose environment hashes, never
environment values. This is current provenance, not fault/paper acceptance.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crystallization_mpc.testing.prosys.runner import source_manifest, utc_now
from integration_tests.prosys.integration_qualification import (
    SERVICES, verify_provenance, manifest_valid,
)


ROOT = Path(__file__).resolve().parents[2]
PROJECT = "mpcrystal-prosys"
REPORTS = Path(".runtime/prosys-test/reports")
NAMES = {service: f"{PROJECT}_{service}_1" for service in sorted(SERVICES)}
AUXILIARY = ("params_default.yaml", "param_meta.yaml")
BUDGET_S = 120.0
SCOPE = ("Fresh read-only Podman identities and independent file-byte hashes in all five app containers. "
         "No service lifecycle, HTTP, OPC UA, control or process-model operation. "
         "This is current runtime provenance, never retrospective deployment or acceptance proof.")


class CollectionError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _json_digest(value):
    return digest(json.dumps(value, sort_keys=True).encode())


def _checked_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CollectionError("Ambiguous duplicate JSON key in Podman output.")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(CollectionError("Nonfinite Podman JSON.")))


def _labels(labels, service):
    for key, value in (("project", PROJECT), ("service", service)):
        found = [labels.get(prefix + key) for prefix in ("io.podman.compose.", "com.docker.compose.")]
        if value not in found or any(item not in (None, value) for item in found):
            raise CollectionError(f"Unexpected or inconsistent compose ownership: {service}.")


def _sanitize(item, service, expected):
    labels = item.get("Config", {}).get("Labels") or {}
    _labels(labels, service)
    state, config = item.get("State", {}), item.get("Config", {})
    if state.get("Running") is not True or state.get("Paused") is True:
        raise CollectionError(f"App container is not running and unpaused: {service}.")
    if item.get("Name", "").lstrip("/") != NAMES[service]:
        raise CollectionError(f"Unexpected container name: {service}.")
    for key in ("Id", "Image"):
        value = item.get(key, "").removeprefix("sha256:")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise CollectionError(f"Invalid actual container/image identity: {service}.")
    environment = {}
    for entry in config.get("Env", []):
        if "=" not in entry:
            raise CollectionError(f"Malformed environment: {service}.")
        key, value = entry.split("=", 1)
        if key in environment:
            raise CollectionError(f"Duplicate environment key: {service}.")
        environment[key] = value
    expected_env = {key: str(value) for key, value in expected["environment"].items()}
    selected = {key: environment.get(key) for key in expected_env}
    if selected != expected_env:
        raise CollectionError(f"Runtime environment differs from isolated compose: {service}.")
    if config.get("Cmd") != expected["command"] or config.get("Entrypoint") not in (None, [], ""):
        raise CollectionError(f"Runtime command/entrypoint differs from isolated compose: {service}.")
    mounts = []
    for mount in item.get("Mounts", []):
        destination = str(mount.get("Destination", "")).rstrip("/")
        if destination in {"/app", "/app/src"} or destination.startswith("/app/src/"):
            raise CollectionError(f"Source mount overrides the app image: {service}.")
        mounts.append({"type": mount.get("Type"), "source": mount.get("Source"),
                       "destination": mount.get("Destination"), "rw": mount.get("RW")})
    return {"id": item["Id"], "name": NAMES[service],
        "image": item["Image"].removeprefix("sha256:"), "state": "running",
        "running": True, "paused": False, "started_at": state.get("StartedAt"),
        "compose_project": PROJECT, "compose_service": service,
        "compose_labels": {key: labels[key] for key in sorted(labels)
            if key in {prefix + suffix for prefix in ("io.podman.compose.", "com.docker.compose.")
                       for suffix in ("project", "service")}},
        "source_mounts_absent": True, "mounts": sorted(mounts, key=lambda value: str(value["destination"])),
        "runtime_configuration": {"environment_keys": sorted(expected_env),
            "environment_sha256": _json_digest(selected),
            "all_environment_sha256": _json_digest(environment),
            "expected_environment_sha256": _json_digest(expected_env),
            "command": deepcopy(config["Cmd"]), "entrypoint": config.get("Entrypoint")}}


def collect(root=ROOT, *, run=subprocess.run, manifest=source_manifest,
            clock=time.monotonic, now=utc_now):
    """Read and cross-check actual five-service state; return two unwritten reports."""
    root = Path(root)
    started, captured_at = clock(), now()
    initial = manifest()
    if not manifest_valid(initial):
        raise CollectionError("Invalid fresh host manifest.")
    compose_path = root / ".runtime/prosys-test/compose.yaml"
    if compose_path.is_symlink() or any(path.is_symlink() for path in compose_path.parents):
        raise CollectionError("Symlink compose configuration is not accepted.")
    raw_compose = compose_path.read_bytes()
    if initial["files"].get(".runtime/prosys-test/compose.yaml") != digest(raw_compose):
        raise CollectionError("Compose bytes disagree with the captured host manifest.")
    configured = yaml.safe_load(raw_compose)["services"]
    expected = {name: value for name, value in initial["files"].items()
        if name.startswith("src/") and (name.endswith(".py") or name.endswith("/app.js"))}
    auxiliary = {name: initial["files"][name] for name in AUXILIARY}
    if not expected:
        raise CollectionError("No host package/UI hashes to compare.")

    def command(argv, timeout):
        remaining = BUDGET_S - (clock() - started)
        if remaining <= 0:
            raise CollectionError("Read-only provenance capture exceeded its 120-second budget.")
        try:
            result = run(argv, check=True, capture_output=True, timeout=min(timeout, remaining))
        except subprocess.CalledProcessError as exc:
            # Never echo stderr/raw inspect, which can contain private environment values.
            raise CollectionError(f"Read-only Podman operation failed with exit code {exc.returncode}.") from None
        except (subprocess.TimeoutExpired, OSError):
            raise CollectionError("Read-only Podman operation timed out or could not execute.") from None
        return _checked_object(result.stdout)

    names = list(NAMES.values())
    before = command(["podman", "inspect", *names], 15)
    if not isinstance(before, list) or len(before) != 5:
        raise CollectionError("Exactly five app inspect records are required.")
    by_name = {item.get("Name", "").lstrip("/"): item for item in before}
    if set(by_name) != set(names):
        raise CollectionError("Podman returned unexpected or duplicate app names.")
    snapshots = [_sanitize(by_name[NAMES[service]], service, configured[service]) for service in NAMES]
    if len({item["id"] for item in snapshots}) != 5 or len({item["image"] for item in snapshots}) != 1:
        raise CollectionError("Five distinct containers must use one identical application image.")

    script = ("import hashlib,json,pathlib; p=pathlib.Path('/app/src'); "
        "files=sorted(p.rglob('*.py'))+[p/'crystallization_mpc/apps/central/ui/static/app.js']+"
        "[pathlib.Path('/app/params_default.yaml'),pathlib.Path('/app/param_meta.yaml')]; "
        "print(json.dumps({str(f.relative_to('/app')):hashlib.sha256(f.read_bytes()).hexdigest() "
        "for f in files if f.is_file()},sort_keys=True))")
    observed = []
    for item in snapshots:
        actual = command(["podman", "exec", item["id"], "python", "-B", "-c", script], 20)
        if not isinstance(actual, dict) or actual != {**expected, **auxiliary}:
            raise CollectionError(f"Image file bytes/coverage do not exactly match host: {item['compose_service']}.")
        observed.append({"container_id": item["id"], "service": item["compose_service"],
            "captured_at": now(), "file_count": len(expected),
            "container_files": {key: actual[key] for key in expected},
            "auxiliary_files": {key: actual[key] for key in auxiliary}, "differences": []})

    after = command(["podman", "inspect", *[item["id"] for item in snapshots]], 15)
    if not isinstance(after, list) or len(after) != 5:
        raise CollectionError("Missing post-hash app identity observations.")
    after_by_id = {item.get("Id"): item for item in after}
    if set(after_by_id) != {item["id"] for item in snapshots}:
        raise CollectionError("App identities changed during hashing.")
    for item in snapshots:
        final = _sanitize(after_by_id[item["id"]], item["compose_service"], configured[item["compose_service"]])
        if final != item:
            raise CollectionError(f"App runtime configuration/identity changed during hashing: {item['compose_service']}.")
        item["identity_stable_during_hashing"] = True
        item["post_capture_inspection"] = final
    ending = manifest()
    if initial != ending or clock() - started > BUDGET_S:
        raise CollectionError("Host source/configuration changed during capture or the capture budget expired.")
    completed = now()
    deployment = {"record_type": "current_runtime_app_deployment", "captured_at": captured_at,
        "capture_completed_at": completed, "scope": SCOPE,
        "expected_source_config_sha256": initial["source_sha256"], "containers": snapshots}
    comparison = {"record_type": "runtime_source_comparison", "captured_at": captured_at,
        "capture_completed_at": completed, "scope": SCOPE, "container_id": snapshots[0]["id"],
        "image_id": snapshots[0]["image"], "file_count": len(expected),
        "host_source_config_sha256": initial["source_sha256"],
        "host_source_config_sha256_end": ending["source_sha256"], "host_source_unchanged": True,
        "host_manifest": initial, "container_files": observed[0]["container_files"],
        "auxiliary_files": auxiliary, "differences": [], "all_containers": observed}
    # Re-use the actual qualification consumer, rather than inventing a PASS field.
    verify_provenance(deployment, comparison, initial)
    return deployment, comparison


def report_path(root, supplied):
    root = Path(root).absolute()
    path = Path(supplied)
    if not path.is_absolute():
        path = root / path
    reports = root / REPORTS
    if (path.suffix != ".json" or not path.is_relative_to(reports) or path.resolve() != path
            or path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
            or not path.parent.is_dir() or path.exists()):
        raise CollectionError("Each report must be a new non-symlink .json path below the isolated reports directory.")
    return path


def write_reports(root, deployment_path, comparison_path, reports):
    paths = [report_path(root, path) for path in (deployment_path, comparison_path)]
    if paths[0] == paths[1]:
        raise CollectionError("Deployment and comparison reports need distinct new paths.")
    # Exclusive creation prevents overwriting history even if paths race after validation.
    for path, document in zip(paths, reports):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, allow_nan=False)
            stream.write("\n")
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        left, right = report_path(ROOT, args.deployment), report_path(ROOT, args.comparison)
        if left == right:
            raise CollectionError("Deployment and comparison outputs must differ.")
        reports = collect()
        paths = write_reports(ROOT, left, right, reports)
    except CollectionError as exc:
        print(json.dumps({"status": "INCOMPLETE", "reason": str(exc),
            "scope": "No runtime/acceptance claim. Inspect current state before retrying."}))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "INCOMPLETE", "reason": "Provenance collection failed; no runtime/acceptance claim. Inspect current state before retrying."}))
        return 2
    print(json.dumps({"status": "COLLECTED", "deployment": str(paths[0]), "comparison": str(paths[1]),
        "container_count": len(reports[0]["containers"]), "package_file_count": reports[1]["file_count"],
        "source_sha256": reports[1]["host_source_config_sha256"], "scope": SCOPE}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
