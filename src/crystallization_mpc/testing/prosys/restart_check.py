"""Capture one explicitly authorized restart of the isolated test Controller.

This is a real, bounded fault test, not a paper acceptance run. It creates a
new owned short-dynamic experiment, waits for two fresh valid outputs, restarts
only its label-verified Controller container once, and makes no start/continue
request after the restart. Evidence and verdict are separate from the runner's
ordinary shutdown/archive report. Nothing executes without --execute-restart.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping

import yaml

from .fault_evidence import evaluate
from .runner import ProsysRunner, RunnerConfig, RunnerError, epoch, finite, source_manifest, utc_now


CONTAINER = "mpcrystal-prosys_controller_1"
PROJECT = "mpcrystal-prosys"
LABELS = ("io.podman.compose.project", "com.docker.compose.project",
          "io.podman.compose.service", "com.docker.compose.service")
# Request only these fields from Podman. In particular, never request or log
# .Config.Env, whose full output includes private credentials.
INSPECT_FORMAT = ('{"Id":{{json .ID}},"Name":{{json .Name}},'
                  '"State":{"StartedAt":{{json .State.StartedAt}}},"Labels":{' +
                  ",".join(json.dumps(key) + ':{{json (index .Config.Labels ' + json.dumps(key) + ')}}'
                           for key in LABELS) + '}}')


def run_command(argv: list[str], *, timeout: float) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout).stdout


def inspect_controller(command_runner: Callable, target: str = CONTAINER) -> dict[str, Any]:
    raw = json.loads(command_runner(["podman", "inspect", "--type", "container", "--format", INSPECT_FORMAT, target], timeout=5))
    if not isinstance(raw, dict):
        raise RunnerError("Container inspection did not return one object.")
    identifier, name, labels = raw.get("Id"), raw.get("Name"), raw.get("Labels")
    if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier)
            or not isinstance(name, str) or name.lstrip("/") != CONTAINER or not isinstance(labels, dict)):
        raise RunnerError("The exact isolated Controller container could not be identified.")
    for suffix, expected in (("project", PROJECT), ("service", "controller")):
        values = [labels.get(key) for key in LABELS if key.endswith("." + suffix)]
        present = [value for value in values if value not in (None, "")]
        if not present or any(value != expected for value in present):
            raise RunnerError("Controller compose project/service labels do not match the isolated test stack.")
    started = (raw.get("State") or {}).get("StartedAt")
    epoch(started)
    return {"Id": identifier, "Name": name, "State": {"StartedAt": started},
            "Labels": {key: labels[key] for key in LABELS if labels.get(key)}}


def save_exclusive(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically publish complete JSON without replacing any existing file."""
    content = json.dumps(document, indent=2, allow_nan=False).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".restart-capture-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # atomic and refuses an existing destination
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def configured_sample_age(path: Path) -> tuple[float, dict[str, str]]:
    """Read only the generated test setting; never expose its private env map."""
    data = path.read_bytes()
    document = yaml.safe_load(data)
    if document.get("name") != PROJECT:
        raise RunnerError("Restart check requires the generated isolated compose project.")
    value = float(document["services"]["controller"]["environment"]["PROCESS_SAMPLE_MAX_AGE_S"])
    if not finite(value) or value <= 0:
        raise RunnerError("Configured process sample maximum age is invalid.")
    return value, {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def run_restart_check(config: RunnerConfig, *, execute_restart: bool,
                      process_sample_max_age_s: float, configuration_source: Mapping[str, Any] | None = None,
                      runner_factory: Callable = ProsysRunner, client: Any = None,
                      command_runner: Callable = run_command,
                      monotonic: Callable[[], float] = time.monotonic,
                      sleep: Callable[[float], None] = time.sleep,
                      now: Callable[[], str] = utc_now,
                      source_provider: Callable = source_manifest,
                      telegraf_reader_factory: Callable | None = None) -> dict[str, Any]:
    if execute_restart is not True:
        raise ValueError("Explicit --execute-restart is required before any experiment mutation.")
    if config.mode != "short-dynamic" or not finite(process_sample_max_age_s) or process_sample_max_age_s <= 0:
        raise ValueError("Restart capture requires short-dynamic mode and the actual process maximum age.")
    runner = runner_factory(config, client=client, monotonic=monotonic, sleep=sleep, now=now)
    captures: list[dict[str, Any]] = []
    interim: list[dict[str, Any]] = []
    violation: list[dict[str, Any]] = []
    attempted, capture_error, result = False, None, None
    reader, reader_worker = None, None
    independent_rounds: list[dict[str, Any]] = []
    independent_complete = False

    def capture(kind: str, phase: str, body: Mapping[str, Any]) -> dict[str, Any]:
        item = {"kind": kind, "phase": phase, "observed_at": now(), "body": deepcopy(body)}
        captures.append(item)
        runner.log.append("restart_capture", {"capture": item})
        return item

    def healthy(status: Mapping[str, Any]) -> bool:
        output = status.get("last_control_output") or {}
        process = (status.get("integrations") or {}).get("process") or {}
        try:
            return (status.get("current_run_id") == runner.run_id
                and epoch(status["started_at"]) == epoch(runner.started_at)
                and status.get("active") is True and status.get("status") == "running"
                and process.get("connected") is True and process.get("sample_valid") is True
                and process.get("missing_fields") == [] and finite(process.get("sample_age_s"))
                and 0 <= process["sample_age_s"] <= process_sample_max_age_s
                and 0 <= epoch(now()) - epoch(process["last_read_at"]) <= process_sample_max_age_s
                and output.get("run_id") == runner.run_id and type(output.get("tick_seq")) is int
                and 0 <= epoch(now()) - epoch(output["computed_at"]) <= 35
                and (output.get("result") or {}).get("valid") is True
                and output.get("process_write_error") is None
                and (output.get("process_write") or {}).get("verified") is True)
        except (KeyError, TypeError, ValueError):
            return False

    def check_plant() -> None:
        # The external process must keep publishing this owned run while the
        # Controller restarts. A healthy-looking retained process value is not
        # evidence that the live external plant is still running.
        plant = runner.call("plant", "GET", "/status")
        runner.log.append("restart_plant_watchdog", {"observed_at": now(), "body": deepcopy(plant)})
        try:
            valid = (plant.get("run_id") == runner.run_id
                and epoch(plant["experiment_started_at"]) == epoch(runner.started_at)
                and plant.get("running") is True and not plant.get("error")
                and finite(plant.get("sample_age_s")) and 0 <= plant["sample_age_s"] <= 5
                and 0 <= epoch(now()) - epoch(plant["heartbeat_at"]) <= 5)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise RunnerError("External plant watchdog failed: owned identity, running state or fresh OPC UA heartbeat is missing.")

    try:
        runner.log.append("restart_check_started", {"execute_restart": True, "container_name": CONTAINER,
            "source_manifest": runner.initial_sources, "configuration_source": configuration_source})
        runner.preflight()
        runner.prepare()
        deadline, ticks = monotonic() + 90, set()
        while monotonic() < deadline:
            runner.observe_once()
            status = runner.latest_status or {}
            if healthy(status):
                ticks.add(status["last_control_output"]["tick_seq"])
                if len(ticks) >= 2:
                    break
            sleep(config.poll_s)
        else:
            raise RunnerError("Two distinct fresh valid Controller outputs were not observed within 90 seconds.")

        before_inspection = inspect_controller(command_runner)
        capture("restart", "before", before_inspection)
        before = runner.call("controller", "GET", "/api/status")
        capture("controller", "before", before)
        if not healthy(before):
            raise RunnerError("Owned Controller is no longer healthy immediately before restart.")
        dt = (before.get("scheduler") or {}).get("controller_dt_s")
        if not finite(dt) or dt <= 0 or 2 * dt + 1 > 85:
            raise RunnerError("Two control periods do not fit the bounded restart observation window.")
        old_tick = before["last_control_output"]["tick_seq"]
        identifier = before_inspection["Id"]
        after_deadline = monotonic() + 90
        runner.log.append("restart_intent", {"container_id": identifier, "timeout_s": 30,
            "command": ["podman", "restart", "--time", "10", identifier], "attempt": 1})
        attempted = True  # no error or uncertain outcome can trigger a retry
        try:
            command_runner(["podman", "restart", "--time", "10", identifier], timeout=30)
            runner.log.append("restart_command_returned", {"container_id": identifier})
        except Exception as exc:
            runner.log.append("restart_command_uncertain", {"error_type": type(exc).__name__,
                "container_id": identifier, "retried": False})

        after_inspection = None
        while monotonic() < after_deadline:
            check_plant()
            observed = inspect_controller(command_runner, identifier)
            if observed["Id"] != identifier:
                raise RunnerError("Controller container identity changed; no replacement container will be targeted.")
            if epoch(observed["State"]["StartedAt"]) > epoch(before_inspection["State"]["StartedAt"]):
                after_inspection = observed
                capture("restart", "after", observed)
                break
            sleep(config.poll_s)
        if after_inspection is None:
            raise RunnerError("No newer process start was observed after the single restart attempt.")
        restarted_at = epoch(after_inspection["State"]["StartedAt"])
        while monotonic() < after_deadline:
            check_plant()
            try:
                status = runner.call("controller", "GET", "/api/status")
            except Exception as exc:
                runner.log.append("restart_status_unavailable", {"error_type": type(exc).__name__})
                check_plant()  # a Controller timeout must not suspend the watchdog
                sleep(config.poll_s)
                continue
            item = {"observed_at": now(), "body": deepcopy(status)}
            interim.append(item)
            runner.log.append("restart_interim_status", item)
            output = status.get("last_control_output") or {}
            resumed = status.get("active") is True or status.get("status") == "running"
            newer_output = (not resumed and type(output.get("tick_seq")) is int and output["tick_seq"] > old_tick
                            and output.get("computed_at") and epoch(output["computed_at"]) >= restarted_at)
            if resumed or newer_output:
                violation.append({"observed_at": item["observed_at"], "active": status.get("active"),
                    "status": status.get("status"), "tick_seq": output.get("tick_seq"),
                    "computed_at": output.get("computed_at")})
            check_plant()  # preserve any observed resume FAIL before aborting on plant loss
            if epoch(item["observed_at"]) - restarted_at >= 2 * dt + 1:
                if status.get("status") == "recovery_required" and not independent_complete:
                    # A confirmation hold intentionally never calls process.connect.
                    # Prove the external read path with its own unique queues,
                    # not retained Controller state or a synthetic availability flag.
                    from .raw_store import timestamp_ns
                    if monotonic() + 3 >= after_deadline:
                        raise RunnerError("Independent Telegraf proof exceeds the bounded restart window.")
                    if reader is None:
                        factory = telegraf_reader_factory or runner.telegraf_reader_factory
                        reader = factory(runner.server_identity)
                        reader_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="restart-telegraf-proof")
                        reader_worker.submit(reader.connect).result(timeout=3)
                        check_plant()
                    row = reader_worker.submit(reader.read).result(timeout=3)
                    runner.log.append("restart_independent_telegraf_round", {"observed_at": now(), "body": deepcopy(row)})
                    stamp = timestamp_ns(row["read_at"])
                    node_ids = runner.server_identity["node_ids"]
                    if (not restarted_at <= epoch(row["read_at"]) <= epoch(now())
                            or epoch(now()) - epoch(row["read_at"]) > process_sample_max_age_s
                            or type(row.get("collection_timestamp_ns")) is not int or row["collection_timestamp_ns"] != stamp
                            or not all(finite(row.get(key)) for key in node_ids)
                            or not all((row.get("status_codes") or {}).get(key) in {"OK (0x0)", "The operation succeeded. StatusGood (0x0)"} for key in node_ids)
                            or not all(type(row.get(key)) is int and row[key] >= 0 for key in ("invalid_message_count", "reconnect_count"))):
                        raise RunnerError("Independent Telegraf proof has stale, incomplete or invalid-quality data.")
                    if independent_rounds and stamp == independent_rounds[-1]["collection_timestamp_ns"]:
                        sleep(.05)
                        continue
                    if independent_rounds and (not 0 < stamp - independent_rounds[-1]["collection_timestamp_ns"] <= 2_500_000_000
                            or any(row[key] != independent_rounds[-1][key] for key in ("invalid_message_count", "reconnect_count"))):
                        raise RunnerError("Independent Telegraf proof is not three consecutive healthy collections.")
                    independent_rounds.append(deepcopy(row))
                    if len(independent_rounds) == 3:
                        capture("telegraf", "after", {"source": "Independent TelegrafRoundReader",
                            "run_id": runner.run_id, "experiment_started_at": runner.started_at,
                            "node_ids": node_ids, "rounds": independent_rounds})
                        independent_complete = True
                    sleep(.05)
                    continue  # obtain another raw Controller status after the independent read
                capture("controller", "after", status)
                break
            sleep(config.poll_s)
        else:
            raise RunnerError("Controller did not provide the bounded post-restart observation window.")
    except (Exception, KeyboardInterrupt) as exc:
        capture_error = {"type": type(exc).__name__, "message": str(exc)}
        runner.log.append("restart_capture_error", capture_error)
    finally:
        # Complete immutable fault evidence first, then always finish only this
        # runner's owned experiment. A blanket stack restart/stop is forbidden.
        try:
            if reader is not None:
                try:
                    reader_worker.submit(reader.close).result(timeout=3)
                except Exception as exc:
                    capture_error = {"type": type(exc).__name__, "message": "Independent reader cleanup failed."}
                    runner.log.append("restart_capture_error", capture_error)
                finally:
                    reader_worker.shutdown(wait=False, cancel_futures=True)
            final_source = source_provider()
            document = {"run_id": runner.run_id, "experiment_started_at": runner.started_at,
                "process_sample_max_age_s": process_sample_max_age_s, "captures": captures,
                "interim_observations": interim, "restart_attempted": attempted,
                "unconfirmed_resume_observations": violation, "capture_error": capture_error,
                "source_manifest": runner.initial_sources, "source_manifest_at_end": final_source,
                "configuration_source": configuration_source, "observed_at": now()}
            document["expected_node_ids"] = (getattr(runner, "server_identity", None) or {}).get("node_ids")
            result = evaluate("controller_restart", document)
            result.update(restart_attempted=attempted, capture_error=capture_error,
                          evidence_file="fault_evidence.json", archive_report="report.json")
            if violation:
                result["status"] = "FAIL"
                result["checks"]["any_interim_unconfirmed_resume"] = {"status": "FAIL",
                    "detail": "A post-restart observation was active/running or produced a new output without confirmation.",
                    "observations": violation}
            if final_source.get("source_sha256") != runner.initial_sources.get("source_sha256"):
                result["status"] = "FAIL"
                result["checks"]["source_unchanged"] = {"status": "FAIL", "detail": "Source/config changed during capture."}
            if attempted and capture_error and result["status"] not in {"FAIL", "INCOMPLETE"}:
                result["status"] = "INCOMPLETE"
            save_exclusive(config.output / "fault_evidence.json", document)
            save_exclusive(config.output / "fault_result.json", result)
        finally:
            try:
                runner.shutdown()
            finally:
                try:
                    # This wrapper deliberately does not set short_completed;
                    # short-dynamic INCOMPLETE is not the restart fault verdict.
                    runner.write_report()
                finally:
                    runner.log.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-restart", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    parser.add_argument("--seed", type=int, default=123)
    args = parser.parse_args(argv)
    if not args.execute_restart:
        parser.error("--execute-restart is required before any experiment mutation")
    maximum_age, attribution = configured_sample_age(Path(".runtime/prosys-test/compose.yaml"))
    config = RunnerConfig(output=args.output, mode="short-dynamic", seed=args.seed,
                          commissioning_evidence=args.commissioning_evidence, telemetry_config=args.telemetry_config)
    result = run_restart_check(config, execute_restart=True, process_sample_max_age_s=maximum_age,
                               configuration_source=attribution)
    print(json.dumps({"status": result["status"], "fault_result": str(config.output / "fault_result.json")}, allow_nan=False))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
