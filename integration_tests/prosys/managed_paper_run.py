"""Opt-in, host-only supervision of the unchanged qualified paper driver.

SIGINT/SIGTERM only latch a request. They never throw out of an in-flight HTTP
mutation, re-enter cleanup, or perform I/O in a signal handler. This is NOT a
resume tool: every invocation requires a new, exclusive output directory.

The two existing paper reports retain their original meanings. The additional
managed report describes supervision/cleanup, not paper or hardware success.
SIGKILL, power loss and uninterruptible kernel I/O cannot be made graceful by a
Python handler. Timed-out auxiliary daemon tasks are explicitly UNKNOWN, never
successful; no HTTP mutation is delegated to those threads.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
from typing import Any, Callable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crystallization_mpc.testing.prosys.runner import HttpReadTimeout, RunnerConfig, RunnerError
from integration_tests.prosys.paper_qualified_run import (
    EXIT_CODES, ExplicitParser, QualifiedPaperRunner,
)


class SignalLatch:
    """Public no-I/O handler; the first reason remains stable on repeat signals."""

    def __init__(self) -> None:
        self.count = 0
        self.first_signal: int | None = None
        self.last_signal: int | None = None

    def handler(self, signum: int, frame: Any = None) -> None:
        # No logging, clocks, locks, allocation of an event, or raised exception.
        if self.first_signal is None:
            self.first_signal = signum
        self.last_signal = signum
        self.count += 1


@contextmanager
def installed_signal_handlers(latch: SignalLatch):
    """Install only in the main thread, and restore both previous handlers."""
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, latch.handler)
        yield latch
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


class ManagedInterrupted(RunnerError):
    pass


class ManagedDeadline(RunnerError):
    """Startup/cleanup budget failure, not the paper's active-run time cap."""


class ActiveRunDeadline(TimeoutError):
    """The configured real-time experiment cap, followed by owned cleanup."""


def _stamp_ns(value: Any) -> int:
    """Exact timezone-qualified identity instant, including nanoseconds."""
    if not isinstance(value, str):
        raise ValueError("Identity start must be a timezone-qualified string.")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if match is None:
        raise ValueError("Malformed identity start.")
    dt = datetime.fromisoformat(match[1] + ("+00:00" if match[3] == "Z" else match[3]))
    delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def _same_start(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return actual is expected
    try:
        return _stamp_ns(actual) == _stamp_ns(expected)
    except (ValueError, TypeError, OverflowError):
        return False


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def process_metadata() -> dict[str, Any]:
    """Read only this process's identity; never inspect environment secrets."""
    result = {"pid": os.getpid(), "ppid": os.getppid(), "sid": os.getsid(0),
              "pgid": os.getpgid(0), "python_executable": sys.executable}
    try:
        result["cgroup"] = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        result["cgroup"] = None
        result["cgroup_error"] = type(exc).__name__
    return result


def _exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    # O_EXCL also rejects an existing symlink; private evidence by default.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class _OwnedClient:
    """Capture authoritative ownership BEFORE base call logs/returns/checkpoints."""

    def __init__(self, client: Any, owner: "ManagedPaperRunner"):
        self.client, self.owner = client, owner

    @property
    def timeout_s(self):
        return self.client.timeout_s

    @timeout_s.setter
    def timeout_s(self, value):
        self.client.timeout_s = value

    def request(self, method, url, payload=None):
        operation = self.owner._before_request(method, url, payload)
        try:
            result = self.client.request(method, url, payload)
        except BaseException as exc:
            self.owner._request_unknown(operation, exc)
            raise
        self.owner._after_response(operation, method, url, payload, result)
        return result

    def get_bytes(self, url):
        return self.client.get_bytes(url)


class ManagedPaperRunner(QualifiedPaperRunner):
    """Use original preparation, observations, phase actions and paper verdicts.

    Changed behavior is exclusively host supervision and stricter shutdown.
    ``latch`` and original clock/client injections support wholly fake tests.
    ``run(install_handlers=False)`` is for an already installed outer context.
    """

    def __init__(self, config, *, latch: SignalLatch | None = None, startup_timeout_s: float = 180,
                 cleanup_timeout_s: float = 300, report_timeout_s: float = 60,
                 metadata_factory: Callable[[], dict] = process_metadata, **kwargs):
        for name, value, limit in (("startup_timeout_s", startup_timeout_s, 180),
                                   ("cleanup_timeout_s", cleanup_timeout_s, 300),
                                   ("report_timeout_s", report_timeout_s, 60)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= limit:
                raise ValueError(f"{name} must be positive and at most {limit} seconds.")
        self.latch = latch if latch is not None else SignalLatch()
        self.startup_timeout_s = float(startup_timeout_s)
        self._startup_deadline: float | None = None
        self._startup_complete = False
        self._active_origin: float | None = None
        self._active_deadline_recorded = False
        self.cleanup_timeout_s, self.report_timeout_s = float(cleanup_timeout_s), float(report_timeout_s)
        self._sleep_raw = kwargs.get("sleep")
        self._cleaning = False
        self._shutdown_attempted = False
        self._has_run = False
        self._cleanup_deadline: float | None = None
        self._auxiliary: list[dict[str, Any]] = []
        self._errors: list[dict[str, Any]] = []
        self._primary_failure: dict[str, Any] | None = None
        self._owned_run: str | None = None
        self._owned_start: str | None = None
        self._owned_created_at: str | None = None
        self._known_runs: set[str] | None = None
        self._previous_selection: str | None = None
        self._create_attempted = False
        self._start_attempted = False
        self._create_unknown = False
        self._start_unknown = False
        self._plant_reset_attempted = False
        self._preflight_controller: tuple | None = None
        self._manual_required = False
        self._journal = None
        self._journal_error: str | None = None
        self._cleanup_result: dict[str, Any] = {"status": "NOT_RUN", "controller_inactive": False,
                                               "finish_attempted": False, "plant_stop_attempted": False}
        super().__init__(config, **kwargs)
        self._sleep_raw = self._sleep_raw or self.sleep
        self.sleep = self._interruptible_sleep
        self.client = _OwnedClient(self.client, self)
        self._process_metadata = metadata_factory()
        self._managed_start_hash = _hash(Path(__file__))
        try:
            _exclusive_json(config.output / "managed_started.json", {
                "schema_version": 1, "scope": "Host supervision only; not a paper verdict",
                "started_at": self.now(), "process": self._process_metadata,
                "managed_tool_sha256": self._managed_start_hash, "original_tools": self.wrapper_hashes,
                "source_manifest": self.initial_sources,
                "qualification_path": str(self.qualification_path),
                "budgets": {"startup_timeout_s": self.startup_timeout_s, "active_maximum_s": config.maximum_run_s,
                            "cleanup_timeout_s": self.cleanup_timeout_s, "report_timeout_s": self.report_timeout_s},
                "automatic_resume": False,
            })
            descriptor = os.open(config.output / "managed_events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._journal = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            self.log.close()  # No service action is possible during construction.
            raise

    def _event(self, kind: str, **payload) -> None:
        try:
            self._journal.write(json.dumps({"recorded_at": self.now(), "kind": kind, **payload}, allow_nan=False) + "\n")
            self._journal.flush()
            os.fsync(self._journal.fileno())
        except BaseException as exc:
            self._journal_error = f"{type(exc).__name__}: {exc}"

    def _error(self, stage: str, exc: BaseException, *, primary: bool = False) -> None:
        item = {"stage": stage, "type": type(exc).__name__, "message": str(exc)}
        self._errors.append(item)
        if self._primary_failure is None:
            self._primary_failure = item
        self._event("managed_error", **item)

    def checkpoint(self, location: str) -> None:
        if self._cleaning:
            return
        if self.latch.first_signal is not None:
            self._event("signal_checkpoint", location=location, signal=self.latch.first_signal,
                        signal_count=self.latch.count, identity=self.identity(), tick=self.last_output_tick)
            raise ManagedInterrupted(f"operator_interrupted: signal {self.latch.first_signal} at {location}")
        if (not self._startup_complete and self._startup_deadline is not None
                and self.monotonic() >= self._startup_deadline):
            raise ManagedDeadline("Managed startup deadline exhausted at " + location)
        if self.origin is not None and self.elapsed() >= self.config.maximum_run_s:
            self.gates.check_timeout(self.elapsed())
            if not self._active_deadline_recorded:
                self._active_deadline_recorded = True
                self._event("active_deadline", location=location, identity=self.identity(),
                            elapsed_s=self.elapsed(), maximum_run_s=self.config.maximum_run_s,
                            terminal_reason=self.gates.terminal_reason)
            raise ActiveRunDeadline("Configured active-run deadline exhausted at " + location)
        if self._journal_error:
            raise RunnerError("Managed ownership journal failed: " + self._journal_error)

    def elapsed(self) -> float:
        # Establish this once from the authoritative start response. The base
        # prepare method also sets origin, but cannot extend our deadline after
        # an in-flight start response or a later wall-clock adjustment.
        if self._active_origin is not None:
            return max(0.0, self.monotonic() - self._active_origin)
        return super().elapsed()

    def _active_remaining(self) -> float | None:
        return None if self.origin is None else self.config.maximum_run_s - self.elapsed()

    def _interruptible_sleep(self, seconds: float) -> None:
        self.checkpoint("before_sleep")
        deadline = self.monotonic() + max(0.0, seconds)
        while self.monotonic() < deadline:
            remaining = deadline - self.monotonic()
            if self._cleaning:
                remaining = min(remaining, self._remaining_cleanup())
            else:
                active = self._active_remaining()
                if active is not None:
                    remaining = min(remaining, active)
                if not self._startup_complete and self._startup_deadline is not None:
                    remaining = min(remaining, self._startup_deadline - self.monotonic())
                if remaining <= 0:
                    self.checkpoint("sleep_deadline")
            self._sleep_raw(min(.25, remaining))
            self.checkpoint("during_sleep")
        self.checkpoint("after_sleep")

    def _remaining_cleanup(self) -> float:
        remaining = (self._cleanup_deadline or self.monotonic()) - self.monotonic()
        if remaining <= 0:
            raise ManagedDeadline("Global managed cleanup deadline exhausted.")
        return remaining

    def _bounded_io(self, operation, *args):
        if self._cleaning:
            remaining = self._remaining_cleanup()
        else:
            self.checkpoint("before_bounded_io")
            budgets = []
            if not self._startup_complete and self._startup_deadline is not None:
                budgets.append(self._startup_deadline - self.monotonic())
            active = self._active_remaining()
            if active is not None:
                budgets.append(active)
            if not budgets:
                return super()._bounded_io(operation, *args)
            remaining = min(budgets)
            if remaining <= 0:
                self.checkpoint("before_io_deadline")
        previous = self.client.timeout_s
        self.client.timeout_s = min(previous, remaining)
        try:
            value = operation(*args)
            if self._cleaning:
                self._remaining_cleanup()
            else:
                self.checkpoint("after_bounded_io")
            return value
        except HttpReadTimeout:
            # A GET whose timeout was clipped to the remaining active budget
            # ends the timed experiment, rather than fabricating a process
            # fault. An earlier read timeout remains an actual read failure.
            if not self._cleaning:
                self.checkpoint("after_read_timeout")
            raise
        finally:
            self.client.timeout_s = previous

    def _before_request(self, method, url, payload):
        # Called immediately before the one HTTP attempt, after request-intent logging.
        self.checkpoint("before_http_attempt")
        if method == "GET":
            return None
        central = self.config.central.rstrip("/")
        operation = None
        if url == central + "/api/experiments" and method == "POST":
            if self._create_attempted or self._known_runs is None or not isinstance(payload, dict) or payload.get("expected_run_id") != self._previous_selection:
                raise RunnerError("Create requires one fresh registry observation and one attempt only.")
            self._create_attempted = True
            operation = "create"
        elif url == central + "/api/operation/experiment/start" and method == "POST":
            if self._start_attempted or not self._owned_run or payload.get("expected_run_id") != self._owned_run:
                raise RunnerError("Start requires confirmed create ownership and one attempt only.")
            self._start_attempted = True
            operation = "start"
        elif url == self.config.plant.rstrip("/") + "/reset" and method == "POST":
            self._plant_reset_attempted = True
            operation = "plant_reset"
        if operation:
            self._event("ownership_intent", operation=operation, identity=self.identity())
            if self._journal_error and not self._cleaning:
                raise RunnerError("Cannot attempt a mutation with a failed ownership journal.")
        return operation

    def _request_unknown(self, operation, exc):
        if operation == "create":
            self._create_unknown = True
        elif operation == "start":
            self._start_unknown = True
        if operation in {"create", "start", "plant_reset"}:
            self._manual_required = True
            self._event("ownership_outcome_unknown", operation=operation, error_type=type(exc).__name__, identity=self.identity())

    def _after_response(self, operation, method, url, payload, result):
        if method == "GET" and url == self.config.central.rstrip("/") + "/api/experiments" and not self._create_attempted:
            entries = result.get("experiments") if isinstance(result, dict) else None
            if not isinstance(entries, list) or any(not isinstance(row, dict) or not isinstance(row.get("run_id"), str) for row in entries):
                raise RunnerError("Registry observation is malformed; no create ownership can be established.")
            self._known_runs = {row["run_id"] for row in entries}
            self._previous_selection = result.get("current_run_id")
        if operation == "create":
            run = result.get("run_id") if isinstance(result, dict) else None
            valid = (isinstance(run, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run)
                     and run not in (self._known_runs or set()) and result.get("selected") is True
                     and result.get("label") == payload.get("label") and result.get("status") == "created"
                     and result.get("started_at") is None and result.get("ended_at") is None)
            try:
                _stamp_ns(result.get("created_at") if isinstance(result, dict) else None)
            except (ValueError, TypeError, OverflowError):
                valid = False
            if not valid:
                self._create_unknown = self._manual_required = True
                self._event("ownership_response_rejected", operation=operation)
                raise RunnerError("Create response did not establish a new, selected, unstarted owned run.")
            self._owned_run = self.run_id = run
            self._owned_created_at = result["created_at"]
            self._event("ownership_confirmed", operation=operation, identity=self.identity())
        elif operation == "start":
            experiment = result.get("experiment") if isinstance(result, dict) else None
            try:
                if not isinstance(experiment, dict) or experiment.get("run_id") != self._owned_run:
                    raise ValueError("Start response belongs to another run.")
                _stamp_ns(experiment.get("started_at"))
                if _stamp_ns(experiment["started_at"]) < _stamp_ns(self._owned_created_at):
                    raise ValueError("Start response precedes this run's creation.")
                if experiment.get("ended_at") is not None:
                    raise ValueError("Start response is already ended.")
            except (ValueError, TypeError, OverflowError):
                self._start_unknown = self._manual_required = True
                self._event("ownership_response_rejected", operation=operation)
                raise RunnerError("Start response omitted a trustworthy owned run/start identity.")
            self._owned_start = self.started_at = experiment["started_at"]
            start_age_s = max(0.0, (_stamp_ns(self.now()) - _stamp_ns(self.started_at)) / 1e9)
            self._active_origin = self.monotonic() - start_age_s
            self.origin = self._active_origin
            self._event("ownership_confirmed", operation=operation, identity=self.identity())

    def call(self, service, method, path, payload=None):
        self.checkpoint(f"before_call:{service}:{method}:{path}")
        result = super().call(service, method, path, payload)
        self.checkpoint(f"after_call:{service}:{method}:{path}")
        return result

    def preflight(self):
        self.checkpoint("before_preflight")
        super().preflight()
        self._preflight_controller = self.preflight_controller_identity
        self.checkpoint("after_preflight")

    def prepare(self):
        self.checkpoint("before_prepare")
        result = super().prepare()
        self.checkpoint("after_prepare")
        return result

    def observe_once(self):
        self.checkpoint("before_observe")
        result = super().observe_once()
        self.checkpoint("after_observe")
        return result

    def stage_action(self, action):
        self.checkpoint("before_stage:" + action)
        result = super().stage_action(action)
        self.checkpoint("after_stage:" + action)
        return result

    def _is_owned(self, document, *, controller=False):
        return (isinstance(document, dict)
                and "started_at" in document
                and document.get("current_run_id" if controller else "run_id") == self._owned_run
                and _same_start(document.get("started_at"), self._owned_start))

    def _controller_inactive(self, status):
        if (status.get("active") is not False or status.get("status") not in {"idle", "configured", "stopped"}
                or (status.get("recovery") or {}).get("requires_confirmation") is True):
            return False
        if self._owned_start is not None:
            try:
                return (self._is_owned(status, controller=True)
                        and _stamp_ns(status.get("stopped_at")) >= _stamp_ns(self._owned_start))
            except (ValueError, TypeError, OverflowError):
                return False
        observed = (status.get("current_run_id"), status.get("started_at"), status.get("status"))
        return (self._is_owned(status, controller=True) or observed == self._preflight_controller)

    def _ended_owned(self, experiment):
        try:
            lower = self._owned_start or self._owned_created_at
            return (self._is_owned(experiment) and experiment.get("status") in {"completed", "error"}
                    and _stamp_ns(experiment.get("ended_at")) >= _stamp_ns(lower))
        except (ValueError, TypeError, OverflowError):
            return False

    def _bounded_aux(self, name: str, operation: Callable, deadline: float):
        """Bound non-mutating annotation/report/close work, never HTTP mutations."""
        outcome: dict[str, Any] = {"name": name, "status": "PENDING"}
        result: list[Any] = []
        errors: list[BaseException] = []

        if deadline <= self.monotonic() and not name.endswith("close"):
            outcome.update(status="NOT_ATTEMPTED_DEADLINE", detail="No new query/report task starts after its shared deadline.")
            self._auxiliary.append(outcome)
            error = ManagedDeadline(name + " was not started after its shared deadline.")
            self._error(name, error)
            raise error

        def invoke():
            try:
                result.append(operation())
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=invoke, name="managed-" + name, daemon=True)
        worker.start()
        worker.join(max(0.0, deadline - self.monotonic()))
        if worker.is_alive():
            outcome.update(status="UNKNOWN_TIMEOUT", detail="Daemon auxiliary may still finish; no success is asserted.")
            error = ManagedDeadline(name + " exceeded its shared deadline.")
        elif errors:
            outcome.update(status="FAIL", error_type=type(errors[0]).__name__)
            error = errors[0]
        else:
            outcome["status"] = "COMPLETED"
            error = None
        self._auxiliary.append(outcome)
        if error is not None:
            self._error(name, error)
            raise error
        return result[0] if result else None

    def shutdown(self):
        """One attempt; unknown/foreign ownership is never guessed or retried."""
        if self._shutdown_attempted:
            return
        self._shutdown_attempted = True
        self._cleaning = True  # All later signals remain latched, never interrupt cleanup.
        self._cleanup_deadline = self.monotonic() + self.cleanup_timeout_s
        try:
            if self._create_unknown or self._start_unknown:
                raise RunnerError("Ambiguous create/start outcome: manual identity reconciliation required; no cleanup mutation sent.")
            if self._owned_run is None:
                self._cleanup_result["status"] = "NOT_NEEDED"
                return
            if self.run_id != self._owned_run or not _same_start(self.started_at, self._owned_start):
                raise RunnerError("Driver identity differs from immutable ownership ledger.")
            current = (self.call("central", "GET", "/api/system/status").get("current_experiment") or {})
            controller = self.call("controller", "GET", "/api/status")
            if not self._is_owned(current):
                raise RunnerError("Central selected another run/start; no finish was sent.")
            if self._owned_start is not None:
                if not self._is_owned(controller, controller=True):
                    raise RunnerError("Controller identity is foreign or unknown; no finish was sent.")
            elif not self._controller_inactive(controller):
                raise RunnerError("Unstarted run no longer has the unchanged inactive Controller boundary.")
            if current.get("status") in {"completed", "error"}:
                if not self._ended_owned(current):
                    raise RunnerError("Owned terminal manifest has no valid end marker; no cleanup mutation sent.")
                self._cleanup_result["finish_skipped_already_ended"] = True
            else:
                self._cleanup_result["finish_attempted"] = True
                self.call("central", "POST", f"/api/experiments/{self._owned_run}/finish", {"expected_run_id": self._owned_run})
            deadline = min(self.monotonic() + self.config.action_timeout_s, self._cleanup_deadline)
            while self.monotonic() < deadline:
                controller = self.call("controller", "GET", "/api/status")
                experiment = self.call("central", "GET", f"/api/experiments/{self._owned_run}")
                if not self._is_owned(experiment):
                    raise RunnerError("Ended experiment run/start changed; no plant stop was sent.")
                if self._owned_start is not None and not self._is_owned(controller, controller=True):
                    raise RunnerError("Controller run/start changed during stop acknowledgement.")
                if self._controller_inactive(controller) and self._ended_owned(experiment):
                    self._cleanup_result["controller_inactive"] = True
                    self._cleanup_result["controller_final_tick"] = (controller.get("last_control_output") or {}).get("tick_seq")
                    self._cleanup_result["experiment_ended_at"] = experiment["ended_at"]
                    self._cleanup_result["controller_stopped_at"] = controller.get("stopped_at")
                    break
                self.sleep(min(self.config.poll_s, max(0.0, deadline - self.monotonic())))
            else:
                raise ManagedDeadline("Finish not positively acknowledged; plant must not be frozen while Controller may still write.")
            if self._plant_reset_attempted:
                plant = self.call("plant", "GET", "/status")
                if (plant.get("run_id") != self._owned_run or "experiment_started_at" not in plant
                        or not _same_start(plant.get("experiment_started_at"), self._owned_start)):
                    raise RunnerError("Plant identity changed; no plant stop was sent.")
                # Cross-service checks cannot be atomic. Re-read immediately
                # before stop to narrow, not conceal, the ownership race.
                final_current = (self.call("central", "GET", "/api/system/status").get("current_experiment") or {})
                final_controller = self.call("controller", "GET", "/api/status")
                if not self._ended_owned(final_current) or not self._controller_inactive(final_controller):
                    raise RunnerError("Central/Controller changed before plant stop; no plant stop was sent.")
                self._cleanup_result["plant_stop_attempted"] = True
                stopped = self.call("plant", "POST", "/stop", self.identity())
                if (stopped.get("run_id") != self._owned_run
                        or "experiment_started_at" not in stopped
                        or not _same_start(stopped.get("experiment_started_at"), self._owned_start)
                        or stopped.get("running") is not False or stopped.get("status") != "stopped"):
                    raise RunnerError("Owned plant stop was not positively acknowledged.")
            self._cleanup_result["status"] = "COMPLETED"
        except BaseException as exc:
            self._cleanup_result["status"] = "MANUAL_ATTENTION"
            self._manual_required = True
            self.shutdown_errors.append(f"{type(exc).__name__}: {exc}")
            self._error("safe_shutdown", exc)
        finally:
            # Acquisition/annotation failure must not skip resource close/report.
            if self.started_at and self._cleanup_result["status"] == "COMPLETED":
                try:
                    self._remaining_cleanup()
                    self._validate_completed_archive()
                    self._remaining_cleanup()
                except BaseException as exc:
                    if self.archive_validation.get("status") in {"PASS", "FAIL"}:
                        # A later supervision deadline cannot erase an already
                        # observed data verdict (especially a validation FAIL).
                        self.archive_validation = {**self.archive_validation,
                            "managed_cleanup_error": {"type": type(exc).__name__, "message": str(exc)}}
                    else:
                        self.archive_validation = {"status": "INCOMPLETE", "reason": str(exc)}
                    self._error("archive", exc)
            if self.phase_events is not None:
                try:
                    self.phase_annotations = self._bounded_aux("phase_verify", self.phase_events.verify, self._cleanup_deadline)
                except BaseException as exc:
                    self.phase_annotations = {"status": "INCOMPLETE", "error_type": type(exc).__name__}
                finally:
                    try:
                        self._bounded_aux("phase_close", self.phase_events.close, self._cleanup_deadline)
                    except BaseException:
                        pass
            try:
                self.log.append("shutdown", {"errors": self.shutdown_errors, "archive": self.archive})
            except BaseException as exc:
                self._error("shutdown_log", exc)
            self._event("cleanup_finished", result=self._cleanup_result, identity=self.identity(), final_tick=self.last_output_tick)

    def run(self, *, install_handlers: bool = True):
        if self._has_run:
            raise RunnerError("Managed runner is single-use; automatic resume/retry is forbidden.")
        self._has_run = True
        if install_handlers:
            with installed_signal_handlers(self.latch):
                return self._run_managed()
        return self._run_managed()

    def _run_managed(self):
        paper_report = None
        self._startup_deadline = self.monotonic() + self.startup_timeout_s
        try:
            self.checkpoint("before_runner_start")
            self.log.append("runner_start", {"config": {**asdict(self.config), "output": str(self.config.output),
                **{name: str(getattr(self.config, name)) if getattr(self.config, name) is not None else None
                   for name in ("commissioning_evidence", "dynamic_evidence", "telemetry_config")}},
                "source_manifest": self.initial_sources})
            self.preflight()
            self.checkpoint("between_preflight_and_prepare")
            self.prepare()
            self.checkpoint("after_complete_prepare")
            self._startup_complete = True
            next_progress = 0.0
            while self.gates.terminal_reason is None:
                action = self.observe_once()
                if action in {"seed_and_E_A", "G_and_all", "steady_control"}:
                    self.stage_action(action)
                elif action is not None:
                    break
                if self.elapsed() >= next_progress:
                    print(f"run={self.run_id} phase={self.gates.phase} elapsed_min={self.elapsed()/60:.1f} tick={self.last_output_tick}", flush=True)
                    next_progress = self.elapsed() + 60
                self.sleep(self.config.poll_s)
        except ManagedInterrupted as exc:
            self.gates.fail("operator_interrupted")
            self._error("active", exc, primary=True)
        except TimeoutError as exc:
            if self.origin is not None and self.elapsed() >= self.config.maximum_run_s:
                self.gates.check_timeout(self.elapsed())
            else:
                self.gates.fail(f"{type(exc).__name__}: {exc}")
                self._error("active", exc, primary=True)
        except BaseException as exc:
            self.gates.fail("operator_interrupted" if isinstance(exc, KeyboardInterrupt) else f"{type(exc).__name__}: {exc}")
            self._error("active", exc, primary=True)
        finally:
            try:
                self.shutdown()
            except BaseException as exc:
                self.shutdown_errors.append(f"{type(exc).__name__}: {exc}")
                self._manual_required = True
                self._error("shutdown_unhandled", exc)
        report_deadline = self.monotonic() + self.report_timeout_s
        managed_end_hash = None
        try:
            managed_end_hash = _hash(Path(__file__))
            if managed_end_hash != self._managed_start_hash:
                raise RunnerError("Managed harness file changed during this invocation.")
        except BaseException as exc:
            self.gates.fail("Managed harness file changed or became unreadable.")
            self._error("managed_provenance", exc, primary=self._primary_failure is None)
        # A signal first observed during cleanup is still not a normal completion.
        if self.latch.first_signal is not None:
            self.gates.fail("operator_interrupted")
        try:
            paper_report = self._bounded_aux("paper_report", self.write_report, report_deadline)
        except BaseException as exc:
            if self._primary_failure is None:
                self._primary_failure = {"stage": "paper_report", "type": type(exc).__name__, "message": str(exc)}
        finally:
            try:
                self._bounded_aux("evidence_close", self.log.close, report_deadline)
            except BaseException:
                pass
        if self._journal_error and not any(item["stage"] == "managed_journal" for item in self._errors):
            self._manual_required = True
            self._error("managed_journal", OSError(self._journal_error))
        completion = "INTERRUPTED" if self.latch.first_signal is not None else "FAILED" if self._errors else "COMPLETED"
        exit_code = (128 + self.latch.first_signal if self.latch.first_signal is not None
                     else 2 if self._errors or paper_report is None
                     else EXIT_CODES.get(paper_report.get("overall"), 4))
        report = {"schema_version": 1, "scope": "Host supervision only; cleanup success is NOT paper success",
                  "managed_completion": completion, "exit_code": exit_code, "completed_at": self.now(),
                  "process": self._process_metadata, "identity": self.identity(),
                  "ownership": {"confirmed_run_id": self._owned_run, "confirmed_started_at": self._owned_start,
                                "create_outcome_unknown": self._create_unknown, "start_outcome_unknown": self._start_unknown},
                  "signal": {"first": self.latch.first_signal, "last": self.latch.last_signal, "count": self.latch.count},
                  "final_observed_tick": self.last_output_tick, "primary_failure": self._primary_failure,
                  "paper_failure_reason": self.gates.failure_reason,
                  "errors": list(self._errors), "cleanup": dict(self._cleanup_result),
                  "manual_attention_required": self._manual_required,
                  "archive_validation": self.archive_validation, "phase_annotations": self.phase_annotations,
                  "paper_overall": paper_report.get("overall") if isinstance(paper_report, dict) else None,
                  "original_tools": self.wrapper_hashes, "managed_tool_sha256": managed_end_hash,
                  "managed_tool_sha256_at_start": self._managed_start_hash,
                  "qualification_sha256": self.qualification_sha256,
                  "auxiliary_tasks": self._auxiliary, "journal_error": self._journal_error,
                  "automatic_resume": False, "artifacts": {}}
        self._event("managed_finished", managed_completion=completion, exit_code=exit_code,
                    signal=report["signal"], final_tick=self.last_output_tick)
        try:
            self._bounded_aux("managed_journal_close", self._journal.close, report_deadline)
        except BaseException as exc:
            report["journal_error"] = f"{type(exc).__name__}: {exc}"
            report["exit_code"] = exit_code if self.latch.first_signal is not None else 2
            report["managed_completion"] = completion if self.latch.first_signal is not None else "FAILED"
        # Hash only files that actually exist; a partial original report is not
        # accepted as a completed report just because it has a readable prefix.
        unstable = set()
        for task in self._auxiliary:
            if task["status"] == "UNKNOWN_TIMEOUT":
                unstable.update({"paper_report": {"report.json", "qualified_report.json"},
                                 "evidence_close": {"evidence.jsonl"},
                                 "managed_journal_close": {"managed_events.jsonl"}}.get(task["name"], set()))
        for name in ("managed_started.json", "managed_events.jsonl", "evidence.jsonl", "report.json", "qualified_report.json"):
            if name in unstable:
                report["artifacts"][name] = {"status": "UNSTABLE_UNKNOWN_WRITER", "sha256": None,
                    "detail": "An auxiliary writer/close did not finish; no immutable final hash asserted."}
                continue
            path = self.config.output / name
            try:
                report["artifacts"][name] = {"sha256": _hash(path), "bytes": path.stat().st_size}
            except OSError as exc:
                report["artifacts"][name] = {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
        report["errors"] = list(self._errors)
        if self._journal_error:
            report["journal_error"] = self._journal_error
            report["manual_attention_required"] = True
            if not any(item["stage"] == "managed_journal" for item in self._errors):
                # The stream is already closed: retain the late journal failure
                # directly rather than trying to append another journal event.
                item = {"stage": "managed_journal", "type": "OSError", "message": self._journal_error}
                self._errors.append(item)
                self._primary_failure = self._primary_failure or item
                report["errors"] = list(self._errors)
                report["primary_failure"] = self._primary_failure
            if self.latch.first_signal is None:
                report["managed_completion"], report["exit_code"] = "FAILED", 2
        self._refresh_signal_summary(report)
        report["snapshot_boundary"] = "Immediately before exclusive managed_report write; consult any late-signal sidecar and external unit terminal status."
        try:
            _exclusive_json(self.config.output / "managed_report.json", report)
        except BaseException as exc:
            report["managed_report_write_error"] = f"{type(exc).__name__}: {exc}"
            report["manual_attention_required"] = True
            report["exit_code"] = report["exit_code"] if self.latch.first_signal is not None else 2
            report["managed_completion"] = "INTERRUPTED" if self.latch.first_signal is not None else "FAILED"
        return self._late_signal_summary(report)

    def _refresh_signal_summary(self, report):
        report["signal"] = {"first": self.latch.first_signal, "last": self.latch.last_signal, "count": self.latch.count}
        if self.latch.first_signal is not None:
            report["exit_code"] = 128 + self.latch.first_signal
            report["managed_completion"] = "INTERRUPTED"

    def _late_signal_summary(self, report):
        previous = dict(report["signal"])
        self._refresh_signal_summary(report)
        if report["signal"] != previous:
            path = self.config.output / "managed_late_signal.json"
            late = {"schema_version": 1, "scope": "Append-only post-summary signal observation, not paper success",
                    "observed_at": self.now(), "identity": self.identity(), "signal": report["signal"],
                    "managed_completion": report["managed_completion"], "exit_code": report["exit_code"],
                    "managed_report_sha256": None,
                    "boundary": "Signal observed after the main summary snapshot. External unit exit remains final arbiter after local handler restoration."}
            try:
                late["managed_report_sha256"] = _hash(self.config.output / "managed_report.json")
                _exclusive_json(path, late)
                report["late_signal_artifact"] = {"path": str(path), "sha256": _hash(path)}
            except BaseException as exc:
                report["late_signal_write_error"] = f"{type(exc).__name__}: {exc}"
                report["manual_attention_required"] = True
            self._refresh_signal_summary(report)
        return report


def main(argv=None):
    parser = ExplicitParser(description=__doc__)
    parser.add_argument("--run-paper", action="store_true")
    parser.add_argument("--integration-qualification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, choices=(123, 456), required=True)
    parser.add_argument("--max-hours", type=float, default=6)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--dynamic-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    parser.add_argument("--cleanup-timeout-seconds", type=float, default=300)
    parser.add_argument("--report-timeout-seconds", type=float, default=60)
    parser.add_argument("--startup-timeout-seconds", type=float, default=180)
    args = vars(parser.parse_args(argv))
    if args.pop("run_paper") is not True:
        parser.error("Explicit --run-paper is required; no service action occurred.")
    qualification = args.pop("integration_qualification")
    cleanup = args.pop("cleanup_timeout_seconds")
    reporting = args.pop("report_timeout_seconds")
    startup = args.pop("startup_timeout_seconds")
    try:
        config = RunnerConfig(mode="paper", **args)
        latch = SignalLatch()
        with installed_signal_handlers(latch):
            runner = ManagedPaperRunner(config, integration_qualification=qualification, latch=latch,
                                        startup_timeout_s=startup, cleanup_timeout_s=cleanup, report_timeout_s=reporting)
            result = runner.run(install_handlers=False)
            result = runner._late_signal_summary(result)
    except (OSError, ValueError, RunnerError) as exc:
        print(json.dumps({"managed_completion": "SETUP_REJECTED", "error_type": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 5
    print(json.dumps({key: result.get(key) for key in ("managed_completion", "exit_code", "paper_overall", "identity", "manual_attention_required", "late_signal_artifact", "late_signal_write_error")}))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
