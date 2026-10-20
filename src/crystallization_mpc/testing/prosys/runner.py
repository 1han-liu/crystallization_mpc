"""Real-time HTTP orchestration of the isolated Prosys paper experiment.

No Controller implementation is imported or advanced here. All experiment
actions use Central's public API; measurements reach the ordinary Controller
through Telegraf/RabbitMQ and commands return through its OPC UA Writer.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from .acceptance import AcceptanceRow, PaperAcceptance


class RunnerError(RuntimeError):
    pass


class HttpReadTimeout(RunnerError):
    def __init__(self, url: str):
        super().__init__(f"Read-only HTTP request timed out: GET {url}")
        self.url = url


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch(value: str) -> float:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RunnerError("Evidence timestamps must include their UTC offset.")
    return parsed.timestamp()


def same_start(value: Any, expected: Any) -> bool:
    """Compare timezone-qualified identity instants, not their Z/+00 spelling."""
    if value is None or expected is None:
        return value is expected
    try:
        return epoch(value) == epoch(expected)
    except (ValueError, TypeError, OverflowError, RunnerError):
        return False


def finite(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def source_manifest() -> dict[str, Any]:
    """Fingerprint working source, including new/uncommitted test modules."""
    root = Path(__file__).resolve().parents[4]
    paths = sorted((root / "src").rglob("*.py"))
    paths.extend(root / name for name in ("requirements.txt", "Dockerfile", "services/mpc/Dockerfile", "params_default.yaml",
                 "param_meta.yaml", "config/telegraf/opcua.conf", "config/telegraf/telegraf.conf",
                 "src/crystallization_mpc/apps/central/ui/static/app.js",
                 ".runtime/prosys-test/params_paper.yaml", ".runtime/prosys-test/compose.yaml",
                 ".runtime/prosys-test/telemetry.json"))
    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in paths if path.is_file()}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        head = None
    return {"git_head": head, "source_sha256": digest, "files": files}


@dataclass(frozen=True)
class RunnerConfig:
    output: Path
    central: str = "http://127.0.0.1:18000"
    controller: str = "http://127.0.0.1:18002"
    plant: str = "http://127.0.0.1:18010"
    gsensor: str = "http://127.0.0.1:18001"
    grafana: str = "http://127.0.0.1:3001"
    seed: int = 123
    max_hours: float = 6.0
    poll_s: float = 1.0
    http_timeout_s: float = 5.0
    action_timeout_s: float = 45.0
    scheduler_timeout_s: float = 35.0
    expected_influx: Mapping[str, str] = field(default_factory=lambda: {
        "url": "http://influxdb:8086", "org": "prosys-test", "bucket": "crystallization",
    })
    commissioning_evidence: Path | None = None
    mode: str = "paper"
    dynamic_evidence: Path | None = None
    telemetry_config: Path = Path(".runtime/prosys-test/telemetry.json")
    archive_timeout_s: float = 90.0
    fault_fixture_scope: str = "growth"

    def __post_init__(self) -> None:
        isolated_ports = {"central": 18000, "controller": 18002, "plant": 18010, "gsensor": 18001, "grafana": 3001}
        for name in ("central", "controller", "plant", "gsensor", "grafana"):
            parsed = urlparse(getattr(self, name))
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.username or parsed.password or parsed.port != isolated_ports[name]
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise ValueError("Runner URLs must use the fixed isolated loopback HTTP ports (18000/18002/18010/18001/3001).")
        if not 0 < self.max_hours <= 6 or not 0 < self.poll_s <= 5:
            raise ValueError("The run is limited to six real hours and polls at most every five seconds.")
        if not 0 <= self.seed < 2**32:
            raise ValueError("Noise seed must be a nonnegative 32-bit integer.")
        if set(self.expected_influx) != {"url", "org", "bucket"} or not all(self.expected_influx.values()):
            raise ValueError("Explicit expected Influx URL, organization and bucket are required.")
        if self.mode not in {"paper", "short-dynamic", "fault-fixture"}:
            raise ValueError("Mode must be paper, short-dynamic, or fault-fixture.")
        if (self.fault_fixture_scope not in {"growth", "static-readonly"}
                or self.fault_fixture_scope == "static-readonly" and self.mode != "fault-fixture"):
            raise ValueError("static-readonly scope is only available to an explicit fault-fixture.")
        if not 0 < self.archive_timeout_s <= 180:
            raise ValueError("Archive readback is bounded to at most 180 seconds.")

    @property
    def maximum_run_s(self) -> float:
        limit = {"paper": 6 * 3600, "short-dynamic": 180, "fault-fixture": 600}[self.mode]
        return min(self.max_hours * 3600, limit)


class JsonHttpClient:
    """One HTTP attempt only, especially for operations with uncertain outcomes."""
    def __init__(self, timeout_s: float = 5) -> None:
        self.timeout_s = timeout_s

    def request(self, method: str, url: str, payload: Any = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request = Request(url, data=data, method=method, headers={
            "Accept": "application/json", "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                document = json.load(response)
        except HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", errors="replace")
            raise RunnerError(f"HTTP {exc.code}: {method} {url}: {detail}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            if method == "GET" and (isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)):
                raise HttpReadTimeout(url) from exc
            raise RunnerError(f"HTTP outcome unavailable: {method} {url}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(document, dict):
            raise RunnerError("HTTP response must be a JSON object.")
        return document

    def get_bytes(self, url: str) -> bytes:
        try:
            with urlopen(Request(url, headers={"Accept": "text/csv"}), timeout=self.timeout_s) as response:
                return response.read(128 * 1024 * 1024 + 1)
        except (URLError, TimeoutError, OSError) as exc:
            raise RunnerError(f"Archive download failed: {type(exc).__name__}") from exc


def archive_experiment_outcome(document: Mapping[str, Any], *, started_at: str,
                               capture: Mapping[str, Any]) -> dict[str, Any]:
    """Separate the actual terminal outcome from completeness of its data.

    A failed experiment may have a complete archive. Its logical error time
    can precede the later operator End/final capture, but cannot precede this
    run or exceed its final capture boundary. Compare exact nanoseconds.
    """
    status, error = document.get("status"), document.get("error")
    if status not in {"completed", "error"}:
        raise RunnerError("Archive needs an actual terminal experiment outcome.")
    if status == "error":
        if not isinstance(error, str) or not error.strip():
            raise RunnerError("An error experiment needs its nonempty original error reason.")
        from .raw_store import timestamp_ns
        try:
            expected_start = timestamp_ns(started_at)
            actual_start = timestamp_ns(document["started_at"])
            ended = timestamp_ns(document["ended_at"])
            capture_end = capture["end_ns"]
            if (actual_start != expected_start or type(capture_end) is not int
                    or not expected_start <= ended <= capture_end):
                raise ValueError("Error time lies outside this run's final capture.")
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise RunnerError("Error experiment needs a timezone-qualified end between its start and final capture.") from exc
    return {"data_only": True, "experiment_status": status, "experiment_error": error}


def validate_archive(document: Mapping[str, Any], files: Mapping[str, bytes], *, run_id: str,
                     started_at: str, node_ids: Mapping[str, str], observed_ticks: set[int],
                     verified_commands: set[str], require_g: bool,
                     observed_results: Mapping[int, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Check downloaded bytes; data PASS never changes the experiment outcome."""
    names = {"parameters.csv", "process_data.csv", "controller_data.csv", "gsensor_data.csv", "events.csv"}
    archive, capture = document.get("archive") or {}, document.get("capture") or {}
    outcome = archive_experiment_outcome(document, started_at=started_at, capture=capture)
    if (document.get("run_id") != run_id or archive.get("run_id") != run_id
            or capture.get("run_id") != run_id
            or epoch(document.get("started_at", "")) != epoch(started_at)
            or archive.get("status") != "completed" or archive.get("errors")
            or archive.get("pending_reasons") or set(files) != names):
        raise RunnerError("Archive identity, finalization, completeness or five-file contract failed.")
    coverage = archive.get("coverage") or {}
    for component in ("controller", "process", "process_events"):
        if (coverage.get(component) or {}).get("status") != "available":
            raise RunnerError(f"Archive lacks complete {component} coverage.")
    if require_g and (coverage.get("gsensor") or {}).get("status") != "available":
        raise RunnerError("Paper archive lacks synthetic G measurements.")
    parsed, evidence = {}, {}
    for name, content in files.items():
        if len(content) > 128 * 1024 * 1024:
            raise RunnerError("Archive CSV exceeds the bounded evidence download size.")
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        if len(rows) != (archive.get("rows") or {}).get(name) or any(row.get("run_id") != run_id for row in rows):
            raise RunnerError(f"Archive {name} row count or run identity differs from its manifest.")
        parsed[name] = rows
        evidence[name] = {"sha256": hashlib.sha256(content).hexdigest(), "rows": len(rows), "bytes": len(content)}
    if not all(parsed[name] for name in ("parameters.csv", "process_data.csv", "controller_data.csv", "events.csv")):
        raise RunnerError("Archive required datasets are empty.")
    ticks = [int(row["tick_seq"]) for row in parsed["controller_data.csv"]]
    last = (coverage.get("controller") or {}).get("expected_last_sequence")
    if type(last) is not int or sorted(ticks) != list(range(1, last + 1)) or not observed_ticks.issubset(ticks):
        raise RunnerError("Archived Controller ticks do not match the final producer watermark and observed outputs.")
    for row in parsed["controller_data.csv"]:
        observed = (observed_results or {}).get(int(row["tick_seq"]))
        if observed and any(not math.isclose(float(row[key]), observed[key], rel_tol=1e-10, abs_tol=1e-10)
                            for key in ("T", "T_j", "c", "T_j_set") if finite(observed.get(key))):
            raise RunnerError("Archived Controller values differ from their observed committed output.")
    start, end = int(capture["start_ns"]), int(capture["end_ns"])
    previous = None
    max_gap_ns = int(float(capture.get("telemetry_interval_s", 1)) * 1.5 * 1e9)
    for row in parsed["process_data.csv"]:
        stamp = int(row["time_ns"])
        if not start <= stamp < end or (previous is not None and stamp <= previous) or stamp - (previous if previous is not None else start) > max_gap_ns:
            raise RunnerError("Archived raw collection timestamps are duplicate, reversed or outside capture.")
        previous = stamp
        if (row.get("complete", "").lower() != "true" or row.get("missing_fields") or row.get("invalid_fields")
                or row.get("device") != capture.get("device") or row.get("temperature_unit") != "degC"):
            raise RunnerError("Archived process collection is not a complete raw Celsius collection.")
        for key, node in node_ids.items():
            if row.get(f"{key}_node_id") != node or row.get(f"{key}_quality") not in {"0", "OK (0x0)", "The operation succeeded. StatusGood (0x0)"}:
                raise RunnerError("Archived process node identity or quality is wrong.")
            if not math.isfinite(float(row[key])):
                raise RunnerError("Archived process value is nonfinite.")
    if end - previous > max_gap_ns:
        raise RunnerError("Archived process data does not cover the capture end boundary.")
    events = parsed["events.csv"]
    for command_id in verified_commands:
        matching = [row for row in events if row.get("command_id") == command_id]
        if not any(row.get("service") == "controller" and row.get("stage") == "receipt" for row in matching) or not any(
            row.get("service") == "opcua_writer" and row.get("stage") == "result" for row in matching):
            raise RunnerError("Verified production command lacks both persisted Writer result and Controller receipt.")
        if not any(row.get("service") == "opcua_writer" and row.get("stage") == "result"
                   and row.get("status") == "success" and row.get("write_verified", "").lower() == "true" for row in matching):
            raise RunnerError("Archived Writer outcome contradicts the observed verified command.")
    if require_g and not parsed["gsensor_data.csv"]:
        raise RunnerError("Paper archive synthetic G dataset is empty.")
    return {"status": "PASS", **outcome, "files": evidence, "observed_tick_count": len(observed_ticks),
            "final_controller_tick": last, "verified_command_count": len(verified_commands),
            "scope": "Data completeness only: downloaded five CSVs, identities, counts, continuous ticks, raw nodes/quality and persisted command outcomes. Not an experiment, paper-control or hardware PASS."}


def temperature_response(candidate: Mapping[str, Any], plant: Mapping[str, Any],
                         process: Mapping[str, Any]) -> dict[str, bool]:
    """Observed response consistent with a real write, not causal identification."""
    baseline, write = candidate.get("process_before") or {}, candidate.get("write") or {}
    try:
        written = epoch(write["written_at"])
        collected, observed = epoch(process["read_at"]), epoch(plant["setpoint_observed_at"])
        before = epoch(baseline["read_at"])
        target = float(write["setpoint_K"])
        if write.get("verified") is not True or not finite(write.get("readback_K")) or abs(write["readback_K"] - target) > .05:
            return {}
        if not (before <= written < collected <= written + 180 and written <= observed <= written + 180
                and abs(collected - observed) <= 5 and written - before <= 5):
            return {}
        fields = ("T", "T_j", "c", "count_middle", "T_j_set")
        if not all(finite(process.get(key)) for key in fields) or not all(
            (process.get("status_codes") or {}).get(key) in {"OK (0x0)", "The operation succeeded. StatusGood (0x0)"} for key in fields):
            return {}
        if not all(finite(value) for value in (target, baseline.get("T"), baseline.get("T_j"),
                   plant.get("observed_setpoint_K"), plant.get("T_K"), plant.get("T_j_K"))):
            return {}
        if abs(process["T_j_set"] - target) > .05 or abs(plant["observed_setpoint_K"] - target) > .05:
            return {}
        delta = target - baseline["T_j"]
        if abs(delta) <= .2:
            return {}
        direction = 1 if delta > 0 else -1
        return {"T_j": direction * (process["T_j"] - baseline["T_j"]) > .05
                    and direction * (plant["T_j_K"] - baseline["T_j"]) > .05,
                "T": direction * (process["T"] - baseline["T"]) > .02
                    and direction * (plant["T_K"] - baseline["T"]) > .02}
    except (KeyError, TypeError, ValueError):
        return {}


def verified_write(candidate: Mapping[str, Any], *, run_id: str, started_at: str) -> bool:
    """Require consistent raw command, successful receipt, and committed write."""
    command, receipt, write = (candidate.get(key) or {} for key in ("command", "receipt", "write"))
    actual = receipt.get("write_result") or {}
    try:
        return bool(
            command.get("run_id") == run_id and command.get("command_id")
            and type(command.get("tick_seq")) is int and command["tick_seq"] > 0
            and epoch(command["run_started_at"]) == epoch(started_at)
            and all(command.get(key) is not None and command.get(key) == receipt.get(key)
                    for key in ("command_id", "run_id", "tick_seq", "run_started_at"))
            and receipt.get("status") == "success" and write.get("verified") is True
            and actual.get("verified") is True
            and all(finite(write.get(key)) and finite(actual.get(key)) and write[key] == actual[key]
                    for key in ("setpoint_K", "setpoint_C", "readback_K"))
            and finite(command.get("setpoint_K")) and command["setpoint_K"] == write["setpoint_K"]
            and math.isclose(write["setpoint_K"] - 273.15, write["setpoint_C"], rel_tol=0, abs_tol=1e-8)
            and abs(write["readback_K"] - write["setpoint_K"]) <= .05
            and write.get("written_at") == actual.get("written_at")
            and epoch(write["written_at"]) >= epoch(started_at)
        )
    except (KeyError, TypeError, ValueError):
        return False


class EvidenceLog:
    def __init__(self, directory: Path, now: Callable[[], str]) -> None:
        directory.mkdir(parents=True, exist_ok=False)
        self.directory, self.now = directory, now
        self.path = directory / "evidence.jsonl"
        self._stream = self.path.open("x", encoding="utf-8")

    def append(self, kind: str, payload: Mapping[str, Any]) -> None:
        self._stream.write(json.dumps({"recorded_at": self.now(), "kind": kind, **payload}, allow_nan=False) + "\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def close(self) -> None:
        self._stream.close()


def normalize_row(status: Mapping[str, Any], plant: Mapping[str, Any], *,
                  run_id: str, started_at: str, elapsed_s: float,
                  observed_at: str) -> AcceptanceRow:
    """Only committed output-aligned fitting evidence is admissible."""
    output = status.get("last_control_output") or {}
    if status.get("current_run_id") != run_id or output.get("run_id") != run_id:
        raise RunnerError("Controller output identity changed.")
    if epoch(status["started_at"]) != epoch(started_at):
        raise RunnerError("Controller experiment start identity changed.")
    if plant.get("run_id") != run_id or epoch(plant["experiment_started_at"]) != epoch(started_at):
        raise RunnerError("Plant output identity changed.")
    age = epoch(observed_at) - epoch(output["computed_at"])
    if age < -2 or age > 35:
        raise RunnerError("Committed Controller output is stale or future-dated.")
    fitting = output.get("adaptation_fitting")
    if not isinstance(fitting, Mapping) or output.get("adaptation_capture_error"):
        raise RunnerError("Missing tick-aligned adaptation snapshot; do not pair a newer fit with an older output.")
    result = output.get("result") or {}
    seed_ids = plant.get("seed_event_ids")
    if not isinstance(seed_ids, list) or len(set(seed_ids)) != len(seed_ids):
        raise RunnerError("Plant seed acknowledgements are malformed.")
    # The elapsed time is reconstructed from the observation's monotonic clock
    # and real output age. Do not use Controller tick * dt (its nominal clock).
    actual_sample_elapsed = max(0.0, elapsed_s - max(0.0, age))
    values = {key: result.get(key) for key in (
        "sigma", "G_model", "G_measure_KF", "E_A", "k_0", "n", "T_j", "T_j_set",
    )}
    return AcceptanceRow(
        tick=output["tick_seq"], elapsed_s=actual_sample_elapsed, **values,
        fit_count=fitting.get("fit_count", 0), fit_status=fitting.get("last_status", "not_run"),
        seed_count=len(seed_ids), seed_consumed=len(seed_ids) == 1,
        valid=result.get("valid") is True and output.get("process_write_error") is None,
    )


class ProsysRunner:
    def __init__(self, config: RunnerConfig, *, client: Any = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], str] = utc_now, phase_events: Any = None,
                 telegraf_reader_factory: Callable[[Mapping[str, Any]], Any] | None = None) -> None:
        self.config, self.client = config, client or JsonHttpClient(config.http_timeout_s)
        self.monotonic, self.sleep, self.now = monotonic, sleep, now
        self.telegraf_reader_factory = telegraf_reader_factory or self._new_telegraf_reader
        self.startup_telegraf_readiness: dict[str, Any] = {"status": "NOT_RUN"}
        self.gates = PaperAcceptance(max_elapsed_s=config.maximum_run_s)
        # Host fault orchestrators may impose a shared earlier deadline across
        # both owned runs. Ordinary short-dynamic/paper behavior is unchanged.
        self.operation_deadline: float | None = None
        self.log = EvidenceLog(config.output, now)
        self.run_id: str | None = None
        self.started_at: str | None = None
        self.origin: float | None = None
        self.last_output_tick: int | None = None
        self.last_new_output = self.monotonic()
        self.server_identity: dict[str, Any] | None = None
        self.latest_status: dict[str, Any] | None = None
        self.latest_plant: dict[str, Any] | None = None
        self.preflight_controller_identity: tuple[Any, Any, Any] | None = None
        self.start_parameter_version: int | None = None
        self.shutdown_errors: list[str] = []
        self.runtime_requests: list[dict[str, Any]] = []
        self.refresh_observations: dict[int, dict[str, Any]] = {}
        self.first_process: dict[str, Any] | None = None
        self.last_process: dict[str, Any] | None = None
        self.writes_verified = 0
        self.archive: dict[str, Any] | None = None
        self.archive_validation: dict[str, Any] = {"status": "NOT_RUN"}
        self.phase_events = phase_events
        self.phase_annotations: dict[str, Any] = {"status": "NOT_RUN"}
        self.recorded_phases: set[int] = set()
        self.observed_ticks: set[int] = set()
        self.observed_results: dict[int, dict[str, Any]] = {}
        self.verified_commands: set[str] = set()
        self.pending_responses: list[dict[str, Any]] = []
        self.paired_responses: list[dict[str, Any]] = []
        self.dynamic_components = {key: "NOT_RUN" for key in ("T_j", "T", "postseed_c", "postseed_G")}
        self.seed_baseline: dict[str, Any] | None = None
        self.postseed_evidence: dict[str, Any] | None = None
        self.imported_dynamic: dict[str, Any] | None = None
        self.short_completed = False
        self.initial_sources = source_manifest()
        self.report_path = config.output / "report.json"

    def _bounded_io(self, operation: Callable, *args):
        deadline = self.operation_deadline if self.config.mode == "fault-fixture" else None
        if deadline is None:
            return operation(*args)
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("The shared fault-fixture deadline expired.")
        previous = getattr(self.client, "timeout_s", None)
        if previous is not None:
            self.client.timeout_s = min(previous, remaining)
        try:
            result = operation(*args)
            if self.monotonic() >= deadline:
                raise TimeoutError("The shared fault-fixture deadline expired during I/O; no retry.")
            return result
        finally:
            if previous is not None:
                self.client.timeout_s = previous

    def call(self, service: str, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        url = getattr(self.config, service).rstrip("/") + path
        request_id = uuid4().hex
        if method != "GET":
            self.log.append("request_intent", {"request_id": request_id, "service": service,
                "method": method, "path": path, "payload": payload})
        try:
            response = self._bounded_io(self.client.request, method, url, payload)
        except Exception as exc:
            self.log.append("request_error", {"request_id": request_id, "service": service,
                "method": method, "path": path, "error": str(exc),
                "mutation_outcome": "unknown_or_rejected_no_automatic_retry" if method != "GET" else "read_failed"})
            raise
        if method != "GET":
            self.log.append("request_response", {"request_id": request_id, "response": response})
        return response

    def preflight(self) -> None:
        system = self.call("central", "GET", "/api/system/status")
        controller = self.call("controller", "GET", "/api/status")
        plant = self.call("plant", "GET", "/status")
        gsensor = self.call("gsensor", "GET", "/status")
        self.log.append("preflight", {"system": system, "controller": controller, "plant": plant, "gsensor": gsensor})
        current = system.get("current_experiment")
        if current and current.get("status") not in {"completed", "error"}:
            raise RunnerError("Another current experiment must not be stopped or overwritten.")
        if controller.get("active") or controller.get("status") == "running":
            raise RunnerError("Controller is already running; refusing to take ownership.")
        if (controller.get("status") == "recovery_required"
                or (controller.get("recovery") or {}).get("requires_confirmation") is True):
            raise RunnerError("Controller safety hold requires End of its existing run before creating a test experiment.")
        if (controller.get("status") in {"error", "configured"}
                and controller.get("current_run_id") and controller.get("started_at")
                and controller.get("stopped_at") is None):
            raise RunnerError("Controller's started experiment has not ended; use End before creating a test experiment.")
        if controller.get("active") is False and controller.get("status") in {"idle", "configured", "stopped"}:
            self.preflight_controller_identity = (
                controller.get("current_run_id"), controller.get("started_at"), controller.get("status"),
            )
        if plant.get("running") or plant.get("status") in {"running", "prepared"}:
            raise RunnerError("Test plant already has an active or prepared run.")
        if gsensor.get("synthetic") is not True or gsensor.get("camera_processing") != "NOT RUN":
            raise RunnerError("The selected G service is not the explicit synthetic test double.")
        adapter = controller.get("adapter") or {}
        if adapter.get("safe_noop") is not False or not str(adapter.get("class", "")).endswith("CrystallizationControllerAdapter"):
            raise RunnerError("The real algorithm adapter is required.")
        integrations = controller.get("integrations") or {}
        process = integrations.get("process") or {}
        if not all(process.get(key) is True for key in ("enabled", "write_enabled", "configured")) or process.get("transport") != "rabbitmq":
            raise RunnerError("Process read/write through RabbitMQ must be explicitly enabled.")
        if not str(process.get("class", "")).endswith("RabbitMqProcessAdapter"):
            raise RunnerError("No local/in-process adapter can substitute for the Prosys route.")
        influx = integrations.get("influxdb") or {}
        if not all(influx.get(key) is True for key in ("enabled", "configured", "writer_ready")):
            raise RunnerError("Controller durable Influx persistence is not ready.")
        if any(influx.get(key) != value for key, value in self.config.expected_influx.items()):
            raise RunnerError("Controller Influx destination does not match the declared isolated test stack.")
        if (controller.get("recovery") or {}).get("status") == "error":
            raise RunnerError("Controller checkpoint recovery has failed.")
        path = self.config.commissioning_evidence
        if path is None:
            raise RunnerError("A passing verify-static commissioning report is required before a long experiment; static checks remain NOT_RUN.")
        commissioning = json.loads(path.read_text(encoding="utf-8"))
        if commissioning.get("action") != "verify-static" or commissioning.get("action_status") != "PASS":
            raise RunnerError("Only a passing verify-static report can authorize the subsequent long test; probe/direct writecheck alone is insufficient.")
        # Validate every self-contained raw database row before *any* creation,
        # parameter mutation or plant reset. prepare() additionally compares the
        # report's frozen identity to the newly probed actual Prosys server.
        self._load_commissioning()
        if self.config.mode == "paper":
            self._load_dynamic_evidence()
            if self.phase_events is None:
                from .phase_events import PhaseEvents
                self.phase_events = PhaseEvents.from_file(self.config.telemetry_config)

    def prepare(self) -> None:
        previous = self.call("central", "GET", "/api/experiments")
        created = self.call("central", "POST", "/api/experiments", {
            "label": f"Prosys {self.config.mode} test / Synthetic G / noise {self.config.seed}",
            "expected_run_id": previous.get("current_run_id"),
        })
        self.run_id = created["run_id"]
        self.log.append("owned_run", {"run_id": self.run_id, "created": created})
        params = self.call("central", "GET", "/api/params")
        defaults = params["defaults"]
        controller = dict(defaults["controller"])
        required = {"sigma_set": .035, "G_set": 3e-8, "params.E_A": 150000,
                    "params.k_0": 2e15, "params.n": 1.75, "m_seed": .001,
                    "T_j_min": 253.15}
        if set(required) - controller.keys():
            raise RunnerError("Central parameter API cannot represent all approved paper initial values.")
        controller.update(required)
        shared = {**defaults["shared"], "dt": 15, "dt_G": 15}
        self.call("central", "POST", "/api/params", {
            "version": params["version"], "shared": shared, "gsensor": defaults["gsensor"],
            "controller": controller, "expected_run_id": self.run_id,
        })
        self.call("central", "PUT", "/api/run-configuration", {
            "run_type": "experiment", "controller_mode": "MPC", "control_target": "sigma",
            "adaptation_enabled": False, "adaptation_mode": "E_A",
            "growth_rate_source": "live_gsensor", "expected_run_id": self.run_id,
        })
        prepared = self.call("plant", "POST", "/reset", {
            "run_id": self.run_id, "experiment_started_at": None,
            "seed": self.config.seed, "parameters": {}, "acknowledge_reset": True,
        })
        reset_completed_at = self.now()
        identity = prepared.get("server_identity") or {}
        if "prosys" not in (str(identity.get("product_name")) + str(identity.get("manufacturer"))).lower() or "simulation" not in str(identity.get("product_name", "")).lower():
            raise RunnerError("The actual OPC UA BuildInfo did not verify Prosys Simulation Server.")
        if not identity.get("software_version") or len(identity.get("node_ids") or {}) != 5:
            raise RunnerError("Incomplete actual Prosys version or node mapping evidence.")
        if identity.get("product_identity_verified") is not True or identity.get("node_contract_verified") is not True:
            raise RunnerError("Prosys identity alone is insufficient: the five writable Double nodes must all verify.")
        self.server_identity = dict(identity)
        if self.imported_dynamic and any((self.imported_dynamic.get("server_identity") or {}).get(key) != identity.get(key)
                for key in ("product_uri", "software_version", "build_number", "server_uris", "namespace_uri", "node_ids")):
            raise RunnerError("Short dynamic evidence belongs to another Prosys identity/node contract.")
        if prepared.get("run_id") != self.run_id or prepared.get("seed_event_ids") != []:
            raise RunnerError("Prepared plant identity/seed state is inconsistent.")
        # The production process adapter connects only during experiment.start.
        # Check the plant's actual UA readback first, then let Controller's start
        # guard wait for its own complete fresh Telegraf collection.
        deadline = self.monotonic() + self.config.action_timeout_s
        while self.monotonic() < deadline:
            status = self.call("plant", "GET", "/status")
            if status.get("run_id") != self.run_id or status.get("error"):
                raise RunnerError("Prepared plant identity or OPC UA connection failed.")
            if finite(status.get("sample_age_s")) and status["sample_age_s"] <= 5 and finite(status.get("observed_setpoint_K")):
                if abs(status["observed_setpoint_K"] - 315.15) > .05:
                    raise RunnerError("Commissioning must prepare the Prosys jacket command at 42 C before this new run.")
                break
            self.sleep(self.config.poll_s)
        else:
            raise RunnerError("No fresh actual Prosys setpoint readback from the prepared plant before start.")
        self._load_commissioning()
        if self.gates.report()["checks"]["integration"]["opcua_read"]["status"] != "PASS":
            raise RunnerError("The static read-path gate has not passed; refusing to start a long experiment.")
        self._probe_fresh_telegraf(reset_completed_at)
        draft = self.call("central", "GET", "/api/params")
        if type(draft.get("version")) is not int or draft["version"] <= 0:
            raise RunnerError("Start requires the exact positive parameter draft version.")
        self.start_parameter_version = draft["version"]
        response = self.call("central", "POST", "/api/operation/experiment/start", {
            **{key: draft[key] for key in ("version", "shared", "gsensor", "controller")},
            "expected_run_id": self.run_id,
        })
        experiment = response.get("experiment") or {}
        if experiment.get("run_id") != self.run_id or not experiment.get("started_at"):
            raise RunnerError("Start response omitted authoritative experiment identity.")
        self.started_at = experiment["started_at"]
        self.origin = self.monotonic() - max(0.0, epoch(self.now()) - epoch(self.started_at))
        self.last_new_output = self.monotonic()
        self.call("plant", "POST", "/start", self.identity())
        self._wait("Controller startup", lambda status, plant: self._controller_started(status), startup_status_retry=True)
        status, _ = self._poll()
        if not self._controller_started(status):
            raise RunnerError("Controller stopped before startup confirmation completed.")
        process = (status.get("integrations") or {}).get("process") or {}
        if process.get("sample_valid") is not True or process.get("missing_fields"):
            raise RunnerError("Controller startup did not establish a fresh complete Telegraf sample.")
        applied = status.get("parameters") or {}
        runtime_configuration = (status.get("runtime_controls") or {}).get("configuration") or {}
        for name, expected in {"sigma_set": .035, "G_set": 3e-8, "E_A": 150000,
                               "k_0": 2e15, "n": 1.75, "m_seed": .001,
                               "dt": 15, "dt_G": 15, "T_j_min": 253.15}.items():
            # In sigma mode the ordinary parameters snapshot deliberately omits
            # the inactive G target. The applied runtime configuration owns
            # that standby value; absence from the mode-specific parameters is
            # not evidence that the requested target was lost.
            actual = (runtime_configuration.get(name) if name == "G_set"
                      else applied.get(name, applied.get("params." + name)))
            if not finite(actual) or not math.isclose(actual, expected, rel_tol=1e-12):
                raise RunnerError(f"Frozen Controller parameter does not match paper baseline: {name}.")
        if (status.get("adaptation") or {}).get("enabled") or status.get("control_target") != "sigma":
            raise RunnerError("Experiment did not start with sigma control and adaptation disabled.")
        self.log.append("started", {"identity": self.identity(), "server_identity": self.server_identity,
            "controller_parameters": applied, "runtime_configuration": runtime_configuration,
            "synthetic_source": True})

    def identity(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "experiment_started_at": self.started_at}

    def elapsed(self) -> float:
        return 0.0 if self.origin is None else max(0.0, self.monotonic() - self.origin)

    @staticmethod
    def _new_telegraf_reader(identity: Mapping[str, Any]):
        from .commissioning import TelegrafRoundReader
        from .opcua_client import ProsysConfig
        parsed = {key: re.fullmatch(r"ns=(\d+);s=(.+)", value) for key, value in identity["node_ids"].items()}
        if len(parsed) != 5 or not all(parsed.values()) or len({item[1] for item in parsed.values()}) != 1:
            raise RunnerError("Fresh Telegraf probe requires five String NodeIds in one verified namespace.")
        config = ProsysConfig(endpoint=identity["endpoint"], namespace_index=int(next(iter(parsed.values()))[1]),
                              identifiers={key: item[2] for key, item in parsed.items()})
        # This is the fixed isolated host mapping, never a production .env
        # fallback. The reader owns unique queues and performs no OPC UA writes.
        return TelegrafRoundReader(config, rabbit_url="amqp://guest:guest@127.0.0.1:5674/%2F",
                                   device_id="prosys-test", startup_timeout_s=3.0)

    def _probe_fresh_telegraf(self, reset_completed_at: str) -> None:
        from .raw_store import timestamp_ns
        reader = self.telegraf_reader_factory(self.server_identity)
        worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prosys-readiness-read")
        deadline = self.monotonic() + 15
        after_ns = timestamp_ns(reset_completed_at)
        rounds: list[dict[str, Any]] = []
        outcome = {"status": "PENDING", "after_reset_at": reset_completed_at, "timeout_s": 15,
                   "node_ids": self.server_identity["node_ids"], "rounds": rounds,
                   "scope": "Fresh Telegraf read path only; not Controller startup or dynamic-response PASS."}
        self.startup_telegraf_readiness = outcome
        def bounded_read(operation):
            try:
                return worker.submit(operation).result(timeout=min(3, max(.001, deadline - self.monotonic())))
            except FutureTimeout as exc:
                raise RunnerError("Fresh Telegraf read exceeded its three-second operation bound.") from exc
        try:
            bounded_read(reader.connect)
            # Each blocking reader operation has a three-second startup bound;
            # leave that budget before issuing it, keeping collection <=15 s.
            while self.monotonic() + 3 <= deadline:
                current = bounded_read(reader.read)
                stamp = timestamp_ns(current["read_at"])
                age = epoch(self.now()) - epoch(current["read_at"])
                if (stamp <= after_ns or not -.001 <= age <= 5 or current.get("collection_timestamp_ns") != stamp
                        or not all(finite(current.get(key)) for key in self.server_identity["node_ids"])
                        or any(type(current.get(key)) is not int or current[key] < 0 for key in ("invalid_message_count", "reconnect_count"))
                        or not all((current.get("status_codes") or {}).get(key) in {"OK (0x0)", "The operation succeeded. StatusGood (0x0)"}
                                   for key in self.server_identity["node_ids"])):
                    raise RunnerError("Fresh Telegraf readiness has stale, incomplete, invalid-quality or pre-reset data.")
                if rounds and stamp == rounds[-1]["collection_timestamp_ns"]:
                    self.sleep(.05)
                    continue
                if rounds and (not 0 < stamp - rounds[-1]["collection_timestamp_ns"] <= 2_500_000_000
                               or any(current.get(key) != rounds[-1].get(key) for key in ("invalid_message_count", "reconnect_count"))):
                    raise RunnerError("Fresh Telegraf readiness did not contain three consecutive healthy rounds.")
                rounds.append(dict(current))
                self.log.append("startup_telegraf_round", {"round": current})
                if len(rounds) == 3:
                    outcome.update(status="PASS", completed_at=self.now())
                    return
                self.sleep(.05)
            raise RunnerError(f"Fresh Telegraf readiness timed out with {len(rounds)}/3 distinct healthy rounds.")
        except Exception as exc:
            outcome.update(status="FAIL", reason=str(exc))
            raise RunnerError(f"Live Telegraf readiness failed before experiment.start: {exc}") from exc
        finally:
            try:
                reader.close()
            except Exception as exc:
                outcome.update(status="FAIL", cleanup_error=type(exc).__name__)
                raise RunnerError("Fresh Telegraf reader could not close its own isolated subscriber safely.") from exc
            finally:
                worker.shutdown(wait=False, cancel_futures=True)
                self.log.append("startup_telegraf_readiness", outcome)

    def _load_dynamic_evidence(self) -> None:
        path = self.config.dynamic_evidence
        if path is None:
            raise RunnerError("A passing short-dynamic report is required before the paper experiment.")
        document = json.loads(path.read_text(encoding="utf-8"))
        if (document.get("mode") != "short-dynamic" or document.get("overall") != "PASS" or (document.get("short_dynamic") or {}).get("status") != "PASS"
                or (document.get("archive_validation") or {}).get("status") != "PASS"
                or document.get("shutdown_errors") or document.get("source_manifest", {}).get("source_sha256") != self.initial_sources["source_sha256"]
                or document.get("source_manifest_at_end", {}).get("source_sha256") != self.initial_sources["source_sha256"]
                or document.get("test_configuration") != {"seed": self.config.seed, "expected_influx": dict(self.config.expected_influx)}):
            raise RunnerError("Short dynamic evidence did not pass with the same frozen source/configuration and completed archive.")
        pairs = (document.get("dynamic_transport") or {}).get("paired_responses") or []
        verified = {key: False for key in ("T", "T_j")}
        for pair in pairs:
            candidate = pair.get("candidate") or {}
            command, receipt = candidate.get("command") or {}, candidate.get("receipt") or {}
            identity = document.get("identity") or {}
            if (not verified_write(candidate, run_id=identity.get("run_id"), started_at=identity.get("experiment_started_at"))
                    or (pair.get("plant") or {}).get("run_id") != identity.get("run_id")
                    or epoch(command.get("run_started_at", "")) != epoch(identity.get("experiment_started_at", ""))
                    or epoch((pair.get("plant") or {}).get("experiment_started_at", "")) != epoch(identity.get("experiment_started_at", ""))):
                raise RunnerError("Short dynamic raw command/receipt identities do not match.")
            components = temperature_response(candidate, pair.get("plant") or {}, pair.get("process_after") or {})
            for key in verified:
                verified[key] = verified[key] or components.get(key, False)
        if not all(verified.values()):
            raise RunnerError("Short dynamic report lacks paired raw temperature response evidence.")
        self.imported_dynamic = document
        self.log.append("short_dynamic_imported", {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "identity": document.get("identity"), "verified_components": verified})

    def _load_commissioning(self) -> None:
        path = self.config.commissioning_evidence
        if path is None:
            self.log.append("commissioning_not_supplied", {"status": "NOT_RUN"})
            return
        document = json.loads(path.read_text(encoding="utf-8"))
        identity = document.get("server_identity") or {}
        if identity.get("node_contract_verified") is not True or identity.get("product_identity_verified") is not True:
            raise RunnerError("Commissioning did not verify the complete Prosys node contract.")
        identity_keys = ("product_uri", "software_version", "build_number", "server_uris", "namespace_uri", "node_ids")
        expected_identity = self.server_identity or identity
        if any(identity.get(key) is None or identity.get(key) != expected_identity.get(key) for key in identity_keys):
            raise RunnerError("Commissioning evidence belongs to a different Prosys endpoint/configuration.")
        # Existing commissioning commands intentionally distinguish direct UA
        # writes from production Writer receipts. Import only the read-path test.
        checks = document.get("checks") or {}
        if document.get("action_status") != "PASS":
            raise RunnerError("The supplied commissioning action did not pass.")
        if document.get("action") == "verify-static":
            required = ("telegraf_five_values_and_kelvin_conversion", "telegraf_change_25C_to_299_15K",
                        "telegraf_raw_influx_persistence")
            rounds = document.get("static_telegraf_rounds") or []
            if not all(checks.get(key) == "PASS" for key in required) or len(rounds) < 60 or not document.get("temperature_restored"):
                raise RunnerError("Read-path PASS lacks the required real commissioning rounds/change/restoration evidence.")
            previous = None
            for item in rounds:
                expected = {"T": 298.15, "T_j": 296.15, "c": .32, "count_middle": 17, "T_j_set": 295.15}
                if any(not finite(item.get(key)) or not math.isclose(item[key], value, abs_tol=1e-8, rel_tol=0) for key, value in expected.items()):
                    raise RunnerError("Commissioning raw static values contradict the declared PASS.")
                timestamp = epoch(item["read_at"])
                if previous is not None and not 0 < timestamp - previous <= 2.5:
                    raise RunnerError("Commissioning static rounds are duplicated, reordered or discontinuous.")
                previous = timestamp
            changed = document.get("changed_temperature_round") or {}
            if not finite(changed.get("T")) or abs(changed["T"] - 299.15) > 1e-8:
                raise RunnerError("Commissioning temperature change has no matching raw Kelvin reading.")
            from .raw_store import RawStoreVerifier, load_test_influx_settings, timestamp_ns
            settings = load_test_influx_settings(self.config.telemetry_config)
            proof = document.get("raw_influx_verification") or {}
            if (proof.get("status") != "PASS" or proof.get("source") != "InfluxDB raw query"
                    or any(proof.get(key) != getattr(settings, key) for key in ("url", "org", "bucket"))
                    or settings.org != self.config.expected_influx["org"] or settings.bucket != self.config.expected_influx["bucket"]
                    or proof.get("node_ids") != identity["node_ids"] or proof.get("device_id") != "prosys-test"
                    or proof.get("measurement") != "crystallizer_raw"):
                raise RunnerError("Commissioning raw Influx evidence targets a different store/device/node contract.")
            captured = [*rounds, changed]
            comparison = RawStoreVerifier(settings, identity["node_ids"], device_id="prosys-test").compare_records(proof.get("records") or [], captured)
            expected_count = 5 * len(captured)
            if (comparison["missing"] or comparison["mismatches"]
                    or comparison["matched_value_count"] != expected_count or comparison["matched_quality_count"] != expected_count
                    or proof.get("round_count") != len(captured)
                    or proof.get("collection_timestamp_ns") != sorted(timestamp_ns(item["read_at"]) for item in captured)):
                raise RunnerError("Commissioning raw Influx rows do not independently verify every captured value/quality at exact nanosecond identity.")
            self.gates.record_check("integration", "opcua_read", "PASS", f"Actual verify-static evidence: {path}; five fields, 60 rounds and Celsius/Kelvin change.")
        elif document.get("action") not in {"probe", "initialize-static", "writecheck", "prepare-dynamic"}:
            raise RunnerError("Unsupported commissioning action.")
        self.log.append("commissioning_imported", {"path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "document": document})

    def _controller_started(self, status: Mapping[str, Any]) -> bool:
        return (status.get("current_run_id") == self.run_id and same_start(status.get("started_at"), self.started_at)
                and status.get("active") is True and status.get("status") == "running")

    def _poll(self, *, startup_wait: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.gates.check_timeout(self.elapsed()) == "timeout":
            raise TimeoutError(f"The {self.config.mode} real-time budget expired ({self.config.maximum_run_s:g} s).")
        status = self.call("controller", "GET", "/api/status")
        plant = self.call("plant", "GET", "/status")
        self.latest_status, self.latest_plant = status, plant
        if self.started_at:
            self._check_running_plant(plant)
            if status.get("current_run_id") != self.run_id or not same_start(status.get("started_at"), self.started_at):
                observed_identity = (status.get("current_run_id"), status.get("started_at"), status.get("status"))
                previous = self.preflight_controller_identity
                same_previous = (previous is not None and observed_identity[0] == previous[0]
                    and same_start(observed_identity[1], previous[1]))
                known_inactive_state = (previous is not None and (
                    (observed_identity[2] == previous[2] and observed_identity[2] in {"idle", "stopped"})
                    or (observed_identity[2] == "configured" and type(status.get("parameter_version")) is int
                        and status["parameter_version"] == self.start_parameter_version)))
                allowed_residue = startup_wait and same_previous and known_inactive_state and status.get("active") is False
                self.log.append("startup_previous_controller_status" if allowed_residue else "controller_identity_mismatch",
                    {"controller": status, "plant": plant, "startup_wait": startup_wait,
                     "preflight_identity": self.preflight_controller_identity, "expected_identity": self.identity(),
                     "mutation_retried": False})
                if allowed_residue:
                    # experiment.start is asynchronous. Only the exact inactive
                    # identity already seen before any mutation may linger in
                    # this bounded startup wait. params.update may meanwhile
                    # configure that identity, but only at our submitted version.
                    # Neither state supplies any new-run evidence.
                    return status, plant
                raise RunnerError("Controller switched to a different experiment.")
            recovery = status.get("recovery") or {}
            if (status.get("status") == "recovery_required"
                    or recovery.get("requires_confirmation") is True):
                reason = (recovery.get("hold_detail") or recovery.get("hold_reason")
                          or recovery.get("reason") or status.get("last_error")
                          or "Operator confirmation is required.")
                self.log.append("controller_safety_hold", {"controller": status, "plant": plant})
                raise RunnerError(f"Controller entered a safety hold; no automatic resume: {reason}")
            if status.get("status") == "error" or (status.get("scheduler") or {}).get("tick_error_count", 0):
                reason = status.get("last_error") or ((status.get("integrations") or {}).get("process") or {}).get("service_last_error")
                self.log.append("controller_terminal_error", {"controller": status, "plant": plant})
                raise RunnerError(f"Controller reported a terminal or scheduler error: {reason or 'No error detail supplied.'}")
            if status.get("status") == "running" and (status.get("scheduler") or {}).get("thread_alive") is not True:
                raise RunnerError("Controller scheduler thread is not running.")
            self._observe_dynamic(status, plant)
        return status, plant

    def _check_running_plant(self, plant: Mapping[str, Any]) -> None:
        if plant.get("run_id") != self.run_id or not plant.get("experiment_started_at") or epoch(plant["experiment_started_at"]) != epoch(self.started_at):
            raise RunnerError("Test plant identity changed while the runner owned the experiment.")
        if not plant.get("running") or plant.get("error"):
            raise RunnerError("Test plant stopped or reported an error.")
        if not finite(plant.get("sample_age_s")) or plant["sample_age_s"] > 5:
            raise RunnerError("Test plant heartbeat is stale; repeated old OPC UA values are not new physics.")

    def _wait(self, label: str, predicate: Callable[[dict, dict], bool], *, startup_status_retry: bool = False) -> tuple[dict, dict]:
        deadline = self.monotonic() + self.config.action_timeout_s
        while self.monotonic() < deadline:
            if startup_status_retry and self.monotonic() + 2 * self.config.http_timeout_s > deadline:
                break
            try:
                status, plant = self._poll(startup_wait=startup_status_retry)
            except HttpReadTimeout as exc:
                if not startup_status_retry or exc.url != self.config.controller.rstrip("/") + "/api/status":
                    raise
                plant = self.call("plant", "GET", "/status")
                self._check_running_plant(plant)
                self.log.append("startup_status_read_retry", {"error": str(exc), "plant": plant,
                    "remaining_s": max(0, deadline - self.monotonic()), "mutation_retried": False})
                self.sleep(self.config.poll_s)
                continue
            self.log.append("action_wait", {"label": label, "controller": status, "plant": plant})
            if (not startup_status_retry or self._controller_started(status)) and predicate(status, plant):
                return status, plant
            self.sleep(self.config.poll_s)
        raise RunnerError(f"Timed out waiting for {label}; no mutation was resent.")

    def runtime_change(self, changes: dict[str, Any]) -> dict[str, Any]:
        status = self.call("controller", "GET", "/api/status")
        if status.get("current_run_id") != self.run_id:
            raise RunnerError("Runtime change ownership mismatch.")
        controls = status["runtime_controls"]
        if controls.get("pending"):
            raise RunnerError("An unrelated runtime request is pending.")
        request = {"run_id": self.run_id, "event_id": uuid4().hex,
                   "expected_revision": controls["revision"], "changes": changes, "requested_at": self.now()}
        self.runtime_requests.append(request)
        self.call("central", "POST", "/api/operation/controller/runtime", request)
        def applied(status, _plant):
            result = (status.get("runtime_controls") or {}).get("last_result") or {}
            if result.get("event_id") != request["event_id"]:
                return False
            if result.get("status") == "rejected" or result.get("accepted") is False:
                raise RunnerError("Runtime request was rejected; no retry or forced transition.")
            return result.get("status") == "applied" and result.get("refreshed") is True
        status, _ = self._wait("runtime configuration and committed refresh", applied)
        controls = status["runtime_controls"]
        if controls.get("revision") != request["expected_revision"] + 1 or any(
            controls["configuration"].get(key) != value for key, value in changes.items()
        ):
            raise RunnerError("Runtime acknowledgment did not match the atomic requested configuration.")
        self._inspect_refresh(status.get("last_control_output") or {})
        return controls["last_result"]

    def stage_action(self, action: str) -> None:
        if self.config.mode != "paper":
            raise RunnerError("Paper stage actions are unavailable outside paper mode.")
        if action == "seed_and_E_A":
            response = self.call("central", "POST", "/api/operation/controller/add-seed", {"expected_run_id": self.run_id})
            event = response["event"]
            event_id = event["event_id"]
            if event.get("run_id") != self.run_id:
                raise RunnerError("Seed response identity mismatch.")
            def seed_recorded(status, _plant):
                seeds = status.get("seed_events") or {}
                if seeds.get("count", 0) > 1:
                    raise RunnerError("Controller has more than one seed event.")
                return seeds.get("count") == 1 and (seeds.get("last") or {}).get("event_id") == event_id
            self._wait("Controller seed acknowledgment", seed_recorded)
            plant = self.call("plant", "POST", "/seed", {**self.identity(), "event_id": event_id, "mass_kg": .001})
            if plant.get("seed_event_ids") != [event_id] or not math.isclose(plant.get("seed_mass_kg", 0), .001, rel_tol=1e-12):
                raise RunnerError("External process did not acknowledge exactly one 1 g seed event.")
            self.seed_baseline = {"plant": plant, "process": ((self.latest_status or {}).get("integrations") or {}).get("process", {}).get("last_state"),
                                  "event_id": event_id, "observed_at": self.now()}
            self.gates.record_check("procedure_actions", "seed_once_1g", "PASS", f"Controller and plant acknowledged {event_id} once; mass 0.001 kg.")
            system = self.call("central", "GET", "/api/system/status")
            activation = system["gsensor"]["activation"]
            revision = activation.get("control_revision", activation.get("revision", 0))
            gs_event = uuid4().hex
            self.call("central", "POST", "/api/operation/gsensor/activation", {
                "run_id": self.run_id, "enabled": True, "expected_revision": revision, "event_id": gs_event,
            })
            self._wait("synthetic G activation", lambda status, _plant:
                (status.get("growth_input") or {}).get("control_event_id") == gs_event
                and (status.get("growth_input") or {}).get("requested_enabled") is True)
            result = self.runtime_change({"adaptation_enabled": True, "adaptation_mode": "E_A"})
            self._record_phase(2, expected_tick=result.get("effective_tick"))
            self.gates.record_check("procedure_actions", "phase2_E_A_only", "PASS", "Committed E_A-only runtime configuration while target remains sigma; parameter constancy checked by the stage gate.")
        elif action == "G_and_all":
            result = self.runtime_change({"control_target": "G", "adaptation_enabled": True, "adaptation_mode": "all"})
            self._record_phase(3, expected_tick=result.get("effective_tick"))
            self.gates.record_check("procedure_actions", "phase3_atomic_G_all", "PASS", f"One committed runtime event {result['event_id']} applied target G and all parameters together.")
        elif action == "steady_control":
            self.log.append("steady_control_entered", {"elapsed_s": self.elapsed(), "run_id": self.run_id})
            self._record_phase(4)
        else:
            raise RunnerError(f"Unsupported stage action {action}.")

    def _record_phase(self, phase: int, *, expected_tick: int | None = None) -> None:
        if self.config.mode != "paper" or phase in self.recorded_phases:
            return
        output = (self.latest_status or {}).get("last_control_output") or {}
        if not self.phase_events or output.get("run_id") != self.run_id or (expected_tick is not None and output.get("tick_seq") != expected_tick):
            raise RunnerError("Paper phase has no matching committed output for a truthful annotation.")
        applied_at = output["computed_at"]
        event = self.phase_events.record(phase=phase, run_id=self.run_id, started_at=self.started_at,
            tick=output["tick_seq"], elapsed_s=max(0, self.elapsed() - (epoch(self.now()) - epoch(applied_at))), applied_at=applied_at)
        self.recorded_phases.add(phase)
        self.log.append("paper_phase_annotation", {"event": event})

    def _inspect_refresh(self, output: Mapping[str, Any]) -> None:
        diagnostics = output.get("diagnostics") or {}
        if diagnostics.get("tick_seq") != output.get("tick_seq") or diagnostics.get("run_id") != self.run_id:
            return
        scalar = diagnostics.get("fields") or {}
        raw = diagnostics.get("raw") or {}
        if scalar.get("diag_refresh_applied") is True:
            tick = output["tick_seq"]
            filters = raw.get("filters") or {}
            result = output.get("result") or {}
            main = (filters.get("main") or {}).get("state")
            expected = [result.get("T"), 0.0, result.get("c"), 0.0]
            state_ok = isinstance(main, list) and len(main) == 4 and all(
                finite(a) and finite(b) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)
                for a, b in zip(main, expected)
            )
            matrices_ok = set(filters) == {"main", "G", "E_A", "k_0", "n"} and all(
                isinstance(item.get("covariance"), list) and item["covariance"]
                and item["covariance"] == item.get("initial_covariance") for item in filters.values()
            )
            integrals_ok = all(scalar.get(key) == 0 for key in ("diag_int_target", "diag_int_dT_dt", "diag_int_T"))
            segment_ok = scalar.get("diag_sample_count") == 0 and scalar.get("diag_segment_start_tick") == tick
            evidence = {"tick": tick, "state_ok": state_ok, "matrices_ok": bool(matrices_ok),
                        "integrals_ok": integrals_ok, "segment_ok": segment_ok,
                        "resumed": False, "diagnostics": diagnostics}
            self.refresh_observations[tick] = evidence
            for key, ok in (("refresh_state", state_ok), ("refresh_covariance", matrices_ok),
                            ("refresh_integrals", integrals_ok), ("refresh_fit_segment", segment_ok)):
                if not ok:
                    self.gates.record_check("filter_refresh", key, "FAIL", f"Refresh tick {tick} has contradictory evidence.")
        else:
            previous = self.refresh_observations.get(output.get("tick_seq", 0) - 1)
            if previous and scalar.get("diag_main_lifecycle") == "updated":
                previous["resumed"] = True
        # Both planned config changes must have observed initialization and a
        # following normal update; a missed snapshot remains INCOMPLETE/NOT_RUN.
        if len(self.refresh_observations) == 2:
            for key, field_name in (("refresh_state", "state_ok"), ("refresh_covariance", "matrices_ok"),
                                    ("refresh_integrals", "integrals_ok"), ("refresh_fit_segment", "segment_ok"),
                                    ("refresh_resume", "resumed")):
                if all(item[field_name] for item in self.refresh_observations.values()):
                    self.gates.record_check("filter_refresh", key, "PASS", "Both committed refresh boundaries have matching raw diagnostic evidence.")

    def _inspect_transport(self, status: Mapping[str, Any], output: Mapping[str, Any]) -> None:
        process = (status.get("integrations") or {}).get("process") or {}
        snapshot = output.get("process_state")
        if snapshot:
            if self.first_process is None:
                self.first_process = dict(snapshot)
            self.last_process = dict(snapshot)
        write = output.get("process_write") or {}
        command = process.get("last_command") or {}
        receipt = process.get("last_write_result") or {}
        if output.get("process_write_attempted") is True and write.get("verified") is True:
            candidate = {"command": dict(command), "receipt": dict(receipt), "write": dict(write)}
            identities = command.get("tick_seq") == output.get("tick_seq") and verified_write(
                candidate, run_id=self.run_id, started_at=self.started_at)
            matching = finite(write.get("readback_K")) and abs(write["readback_K"] - output["result"]["T_j_set"]) <= .05
            if identities and matching:
                self.writes_verified += 1
                self.verified_commands.add(command["command_id"])
                if snapshot and write.get("written_at"):
                    self.pending_responses.append({**candidate, "process_before": dict(snapshot), "tick": output["tick_seq"]})
                    self.pending_responses = self.pending_responses[-50:]
                self.gates.record_check("integration", "opcua_write_readback", "PASS", f"Production Writer command/receipt identities and verified OPC UA readback match committed tick {output['tick_seq']}.")
            elif command.get("tick_seq") == receipt.get("tick_seq") == output.get("tick_seq"):
                self.gates.record_check("integration", "opcua_write_readback", "FAIL", "The same-tick committed write contradicts its production command or successful Writer receipt.")
        # Store dynamic evidence without manufacturing commissioning/fault PASS.
        self.log.append("transport_observation", {"tick": output.get("tick_seq"),
            "process_state": snapshot, "write": write, "command": command, "receipt": receipt,
            "verified_writes_total": self.writes_verified})

    def _observe_dynamic(self, status: Mapping[str, Any], plant: Mapping[str, Any]) -> None:
        process = ((status.get("integrations") or {}).get("process") or {}).get("last_state") or {}
        for candidate in self.pending_responses:
            components = temperature_response(candidate, plant, process)
            new = {key: value for key, value in components.items() if value and self.dynamic_components[key] != "PASS"}
            if new:
                pair = {"candidate": candidate, "plant": dict(plant), "process_after": dict(process),
                        "components": components, "scope": "Synthetic temperature response consistent with the observed write; not causal isolation/control improvement."}
                self.paired_responses.append(pair)
                self.log.append("paired_dynamic_response", pair)
                for key in new:
                    self.dynamic_components[key] = "PASS"
        baseline = self.seed_baseline
        output = status.get("last_control_output") or {}
        if baseline and plant.get("seed_event_ids") == [baseline["event_id"]] and finite(plant.get("seed_mass_kg")) and math.isclose(plant["seed_mass_kg"], .001):
            before_process, before_plant = baseline.get("process") or {}, baseline["plant"]
            if (finite(process.get("c")) and finite(before_process.get("c")) and finite(plant.get("c")) and finite(before_plant.get("c"))
                    and before_process["c"] - process["c"] > .0006 and before_plant["c"] - plant["c"] > .0006
                    and process.get("read_at") and epoch(process["read_at"]) > epoch(baseline["observed_at"])):
                self.dynamic_components["postseed_c"] = "PASS"
            result = output.get("result") or {}
            if (output.get("run_id") == self.run_id and output.get("computed_at") and epoch(output["computed_at"]) > epoch(baseline["observed_at"])
                    and result.get("valid") is True and finite(result.get("G_measure")) and result["G_measure"] > 0
                    and finite(plant.get("G_true_m_s")) and plant["G_true_m_s"] > 0):
                self.dynamic_components["postseed_G"] = "PASS"
            if self.postseed_evidence is None and all(self.dynamic_components[key] == "PASS" for key in ("postseed_c", "postseed_G")):
                self.postseed_evidence = {"baseline": baseline, "plant": dict(plant), "process": dict(process), "output": output}
                self.log.append("postseed_process_response", self.postseed_evidence)
        thermal_ok = all(self.dynamic_components[key] == "PASS" for key in ("T", "T_j")) or self.imported_dynamic is not None
        if thermal_ok and all(self.dynamic_components[key] == "PASS" for key in ("postseed_c", "postseed_G")):
            self.gates.record_check("integration", "process_response", "PASS", "Synthetic-only paired Writer/readback/temperature response plus acknowledged 1 g seed, subsequent external c decrease and nonzero G; not physical hardware validation.")

    def observe_once(self) -> str | None:
        status, plant = self._poll()
        output = status.get("last_control_output") or {}
        tick = output.get("tick_seq")
        if tick is None or tick == self.last_output_tick:
            if self.monotonic() - self.last_new_output > self.config.scheduler_timeout_s:
                raise RunnerError("Controller produced no fresh committed output within the scheduler watchdog.")
            return None
        if self.last_output_tick is not None and tick < self.last_output_tick:
            raise RunnerError("Controller output tick moved backwards; implicit restart is forbidden.")
        row = normalize_row(status, plant, run_id=self.run_id, started_at=self.started_at,
                            elapsed_s=self.elapsed(), observed_at=self.now())
        self.last_output_tick, self.last_new_output = tick, self.monotonic()
        self.observed_ticks.add(tick)
        self.observed_results[tick] = {key: output.get("result", {}).get(key) for key in ("T", "T_j", "c", "T_j_set")}
        self._inspect_refresh(output)
        self._inspect_transport(status, output)
        self.log.append("control_sample", {"normalized": asdict(row), "controller": status,
            "plant": plant, "phase_before": self.gates.phase})
        if self.config.mode != "paper":
            return None
        self._record_phase(1)
        action = self.gates.observe(row)
        if action:
            self.log.append("gate_action", {"action": action, "tick": tick, "elapsed_s": row.elapsed_s})
        return action

    def shutdown(self) -> None:
        """Stop only identities this runner created; never use a blanket reset."""
        if not self.run_id:
            if self.phase_events is not None:
                self.phase_events.close()
            return
        try:
            system = self.call("central", "GET", "/api/system/status")
            current = system.get("current_experiment") or {}
            if current.get("run_id") == self.run_id:
                self.call("central", "POST", f"/api/experiments/{self.run_id}/finish", {"expected_run_id": self.run_id})
                deadline = self.monotonic() + self.config.action_timeout_s
                while self.monotonic() < deadline:
                    controller = self.call("controller", "GET", "/api/status")
                    experiment = self.call("central", "GET", f"/api/experiments/{self.run_id}")
                    if controller.get("current_run_id") not in {None, self.run_id} and controller.get("active"):
                        raise RunnerError("Another Controller run became active during shutdown.")
                    if not controller.get("active") and experiment.get("status") in {"completed", "error"}:
                        break
                    self.sleep(self.config.poll_s)
                else:
                    raise RunnerError("Owned experiment stop was sent but completion was not acknowledged.")
                self.archive = self.call("central", "GET", f"/api/experiments/{self.run_id}/archive")
            else:
                raise RunnerError("Central selected another run; did not stop it.")
        except Exception as exc:
            self.shutdown_errors.append(str(exc))
        try:
            plant = self.call("plant", "GET", "/status")
            if plant.get("run_id") == self.run_id and plant.get("experiment_started_at") and self.started_at and epoch(plant["experiment_started_at"]) == epoch(self.started_at):
                self.call("plant", "POST", "/stop", self.identity())
            elif plant.get("run_id") == self.run_id and self.started_at is None and plant.get("experiment_started_at") is None:
                self.call("plant", "POST", "/stop", self.identity())
            else:
                raise RunnerError("Plant identity changed; did not stop another run.")
        except Exception as exc:
            self.shutdown_errors.append(str(exc))
        if self.started_at and not self.shutdown_errors:
            self._validate_completed_archive()
        if self.phase_events is not None:
            try:
                self.phase_annotations = self.phase_events.verify()
            except Exception as exc:
                self.phase_annotations = {"status": "FAIL", "error_type": type(exc).__name__}
            finally:
                self.phase_events.close()
        self.log.append("shutdown", {"errors": self.shutdown_errors, "archive": self.archive})

    def _validate_completed_archive(self) -> None:
        deadline = self.monotonic() + self.config.archive_timeout_s
        try:
            while self.monotonic() < deadline:
                self.archive = self.call("central", "GET", f"/api/experiments/{self.run_id}/archive")
                state = (self.archive.get("archive") or {}).get("status")
                if state in {"completed", "partial", "failed"}:
                    break
                self.sleep(min(2, self.config.poll_s))
            else:
                self.archive_validation = {"status": "INCOMPLETE", "reason": "Archive did not finish within bounded read-only wait.",
                    "observed_status": (self.archive or {}).get("archive")}
                return
            files = {}
            for name in ("parameters.csv", "process_data.csv", "controller_data.csv", "gsensor_data.csv", "events.csv"):
                expected = f"/api/experiments/{self.run_id}/exports/{name}"
                if (self.archive.get("downloads") or {}).get(name) != expected:
                    raise RunnerError("Archive download URL is absent or does not belong to the owned run.")
                content = self._bounded_io(self.client.get_bytes, self.config.central.rstrip("/") + expected)
                if not isinstance(content, bytes) or len(content) > 128 * 1024 * 1024:
                    raise RunnerError("Archive response is not a bounded CSV byte stream.")
                files[name] = content
                destination = self.config.output / name
                with destination.open("xb") as stream:
                    stream.write(content)
            self.archive_validation = validate_archive(self.archive, files, run_id=self.run_id, started_at=self.started_at,
                node_ids=(self.server_identity or {}).get("node_ids") or {}, observed_ticks=self.observed_ticks,
                verified_commands=self.verified_commands,
                require_g=self.config.mode == "paper" or (self.config.mode == "fault-fixture" and self.config.fault_fixture_scope == "growth"),
                observed_results=self.observed_results)
        except Exception as exc:
            self.archive_validation = {"status": "FAIL", "reason": str(exc)}
        finally:
            self.log.append("archive_validation", self.archive_validation)

    def write_report(self) -> dict[str, Any]:
        final_sources = source_manifest()
        if final_sources["source_sha256"] != self.initial_sources["source_sha256"]:
            self.gates.fail("Source changed during the experiment; results are not attributable to one implementation.")
        report = self.gates.report()
        report.update(identity=self.identity(), mode=self.config.mode, source="Synthetic G / model-generated external process",
            server_identity=self.server_identity, runtime_requests=self.runtime_requests,
            refresh_observations=list(self.refresh_observations.values()),
            dynamic_transport={"verified_writes": self.writes_verified,
                "first_process": self.first_process, "last_process": self.last_process,
                "components": self.dynamic_components, "paired_responses": self.paired_responses,
                "postseed_evidence": self.postseed_evidence,
                "short_dynamic_imported_from": str(self.config.dynamic_evidence) if self.imported_dynamic else None},
            archive=self.archive, archive_validation=self.archive_validation, phase_annotations=self.phase_annotations,
            startup_telegraf_readiness=self.startup_telegraf_readiness,
            test_configuration={"seed": self.config.seed, "expected_influx": dict(self.config.expected_influx)},
            shutdown_errors=self.shutdown_errors,
            evidence_jsonl=str(self.log.path), completed_at=self.now(),
            source_manifest=self.initial_sources, source_manifest_at_end=final_sources)
        if self.archive is not None:
            report["experiment_outcome"] = {key: self.archive.get(key) for key in ("status", "error", "ended_at")}
            # An actual ended error run can have complete data. Never turn its
            # data-only PASS into short/paper success if the error raced the
            # final observation. Explicit fault fixtures retain their separate
            # outer safety verdict and ordinary INCOMPLETE scope below.
            if (self.config.mode != "fault-fixture" and self.archive.get("status") == "error"
                    and self.archive.get("ended_at")):
                report["overall"] = "FAIL"
        if self.shutdown_errors:
            report["overall"] = "FAIL"
        artifacts = [self.archive_validation["status"]]
        if self.config.mode == "paper":
            artifacts.append(self.phase_annotations["status"])
        report["results"]["artifacts"] = "FAIL" if "FAIL" in artifacts else "PASS" if all(x == "PASS" for x in artifacts) else "INCOMPLETE"
        if "FAIL" in artifacts:
            report["overall"] = "FAIL"
        elif report["overall"] != "FAIL" and report["results"]["artifacts"] != "PASS":
            report["overall"] = "INCOMPLETE"
        if self.config.mode == "short-dynamic":
            passed = (self.short_completed and report["overall"] != "FAIL" and not self.gates.failure_reason
                      and not self.shutdown_errors and self.archive_validation["status"] == "PASS"
                      and all(report["checks"]["integration"][key]["status"] == "PASS" for key in ("opcua_read", "opcua_write_readback")))
            report["short_dynamic"] = {"status": "PASS" if passed else "FAIL" if report["overall"] == "FAIL" else "INCOMPLETE",
                "scope": "No-seed synthetic temperature-response route only; c consumption, G adaptation, paper phases and hardware are NOT_RUN.",
                "completed_temperature_observation": self.short_completed, "maximum_real_duration_s": 180}
            report["overall"] = report["short_dynamic"]["status"]
            report["results"]["procedure"] = "NOT_RUN"
            report["results"]["control_behavior"] = "NOT_RUN"
        elif self.config.mode == "fault-fixture":
            growth_fixture = self.config.fault_fixture_scope == "growth"
            if not growth_fixture:
                report["source"] = "Static Prosys read-only-node fault fixture; no external plant or Synthetic G"
            report["fault_fixture"] = {
                "kind": self.config.fault_fixture_scope,
                "scope": ("G-control fault fixture: one seed, unchanged 120 s Synthetic G warmup, sigma-to-G switch, adaptation disabled. Not a paper or no-seed short-dynamic experiment."
                    if growth_fixture else "Static read-only-node fault fixture: normal Central/Controller/Writer rejection path, without external plant, seed or G. Not a dynamic or paper experiment."),
                "maximum_run_duration_s": self.config.maximum_run_s,
                "elapsed_s": self.elapsed(),
                "fault_verdict": "See the enclosing fault_result.json; this archive report is not a fault verdict.",
                "requires_actual_G_archive": growth_fixture,
            }
            report["results"]["procedure"] = "NOT_RUN"
            report["results"]["procedure_gates"] = "NOT_RUN"
            report["results"]["control_behavior"] = "NOT_RUN"
            if report["overall"] != "FAIL":
                report["overall"] = "INCOMPLETE"
        if self.started_at:
            report["grafana_url"] = self.config.grafana.rstrip("/") + "/d/crystallization-mpc-analysis/crystallization-mpc-analysis?" + urlencode({
                "orgId": 1, "var-run_id": self.run_id, "from": int(epoch(self.started_at) * 1000), "to": int(epoch(self.now()) * 1000),
            })
        with self.report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
        return report

    def run(self) -> dict[str, Any]:
        if self.config.mode == "fault-fixture":
            raise RunnerError("fault-fixture requires an explicit bounded fault orchestrator; no automatic paper stages.")
        self.log.append("runner_start", {"config": {**asdict(self.config), "output": str(self.config.output),
            **{name: str(getattr(self.config, name)) if getattr(self.config, name) is not None else None
               for name in ("commissioning_evidence", "dynamic_evidence", "telemetry_config")}},
            "source_manifest": self.initial_sources})
        try:
            self.preflight()
            self.prepare()
            next_progress = 0.0
            while self.gates.terminal_reason is None:
                action = self.observe_once()
                if self.config.mode == "short-dynamic":
                    if all(self.dynamic_components[key] == "PASS" for key in ("T", "T_j")):
                        self.short_completed = True
                        break
                    if self.elapsed() >= 180:
                        break
                if action in {"seed_and_E_A", "G_and_all", "steady_control"}:
                    self.stage_action(action)
                elif action is not None:
                    break
                if self.elapsed() >= next_progress:
                    print(f"run={self.run_id} phase={self.gates.phase} elapsed_min={self.elapsed()/60:.1f} tick={self.last_output_tick}", flush=True)
                    next_progress = self.elapsed() + 60
                self.sleep(self.config.poll_s)
        except TimeoutError:
            self.gates.check_timeout(self.config.max_hours * 3600)
        except KeyboardInterrupt:
            self.gates.fail("operator_interrupted")
        except Exception as exc:
            self.gates.fail(f"{type(exc).__name__}: {exc}")
            self.log.append("run_error", {"error": str(exc), "type": type(exc).__name__})
        finally:
            self.shutdown()
        try:
            return self.write_report()
        finally:
            self.log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory; existing paths are never overwritten.")
    for service, default in (("central", 18000), ("controller", 18002), ("plant", 18010), ("gsensor", 18001), ("grafana", 3001)):
        parser.add_argument(f"--{service}", default=f"http://127.0.0.1:{default}")
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--max-hours", type=float, default=6)
    parser.add_argument("--commissioning-evidence", type=Path)
    parser.add_argument("--mode", choices=("paper", "short-dynamic"), default="paper")
    parser.add_argument("--dynamic-evidence", type=Path, help="Passing short-dynamic report required before paper mode.")
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    parser.add_argument("--influx-url", default="http://influxdb:8086")
    parser.add_argument("--influx-org", default="prosys-test")
    parser.add_argument("--influx-bucket", default="crystallization")
    args = vars(parser.parse_args())
    expected_influx = {key: args.pop(f"influx_{key}") for key in ("url", "org", "bucket")}
    report = ProsysRunner(RunnerConfig(**args, expected_influx=expected_influx)).run()
    print(json.dumps({"overall": report["overall"], "report": str(Path(args["output"]) / "report.json"), "grafana_url": report.get("grafana_url")}), flush=True)
    return 0 if report["overall"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
