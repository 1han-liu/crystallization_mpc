"""Four-phase, accelerated numerical acceptance using the real Controller service.

This is an acceptance driver, not a replacement controller or an image sensor.
It sends the usual validated messages and advances the service's existing tick
entry point. Durations are simulation seconds; all persisted timestamps remain
real UTC. No historical experiment or database data is deleted.
"""
from __future__ import annotations

import copy
import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode
from uuid import uuid4

from crystallization_mpc.apps.controller.algorithm.integration import CrystallizationControllerAdapter
from crystallization_mpc.apps.central.runtime_telemetry import RUNTIME_EVENT_MEASUREMENT, write_runtime_event
from crystallization_mpc.apps.controller.algorithm.parameters import build_parameters
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.apps.controller.service import ControllerService
from crystallization_mpc.messaging.commands import (
    CONTROLLER_ADD_SEED_COMMAND, CONTROLLER_RUNTIME_UPDATE_COMMAND,
    EXPERIMENT_START_COMMAND, EXPERIMENT_STOP_COMMAND, PARAMS_UPDATE_MESSAGE,
)
from crystallization_mpc.messaging.schema import build_envelope, utc_ts


PHASE_NAMES = {
    1: "Onset Preparation", 2: "Preliminary Adjustment",
    3: "Status Adjustment", 4: "Control Operation",
}
NOT_RUN = {
    "GSensor image warmup": "Numerical Simulated input is not a camera; eight-frame warmup and imaging latency are not tested.",
    "FBRM counts": "No physical FBRM instrument is connected.",
    "Physical seed dissolution": "Seed events and numerical consumption are tested, not physical dissolution.",
    "Nucleation and full crystal size distribution": "Not a control objective and not represented by this simulation.",
}


def acceptance_parameters() -> dict[str, Any]:
    """Explicit paper/controller guesses; independent process defaults retained."""
    return build_parameters({
        "mode": "MPC", "run_type": "simulation", "growth_rate_source": "simulated",
        "target": "sigma", "sigma_set": 0.035, "G_set": 3e-8,
        "dt": 15.0, "dt_G": 15.0, "E_A": 150000.0, "k_0": 2e15,
        "n": 1.75, "m_seed": 0.001,
    })


@dataclass
class ContinuousGate:
    duration_s: float = 60.0
    since: float | None = None

    def observe(self, satisfied: bool, elapsed_s: float) -> bool:
        if not satisfied:
            self.since = None
            return False
        if self.since is None:
            self.since = elapsed_s
        return elapsed_s - self.since >= self.duration_s


@dataclass
class SuccessfulFitGate:
    """Flat values without a new successful fit must never satisfy this gate."""
    keys: tuple[str, ...]
    threshold: float = 0.10
    required_updates: int = 4
    previous: dict[str, float] | None = None
    last_fit_count: int = 0
    stable_updates: int = 0
    last_fit_elapsed_s: float | None = None

    def observe(self, fit_count: int, values: Mapping[str, Any], elapsed_s: float) -> bool:
        if fit_count <= self.last_fit_count:
            # A failed/missing fit breaks a consecutive-success sequence.
            self.stable_updates = 0
            self.previous = None
            self.last_fit_elapsed_s = None
            return False
        self.last_fit_count = fit_count
        current = {key: float(values[key]) for key in self.keys}
        finite_positive = all(math.isfinite(v) and v > 0 for v in current.values())
        if not finite_positive:
            self.stable_updates = 0
            self.previous = None
            return False
        consecutive = (
            self.last_fit_elapsed_s is not None
            and elapsed_s - self.last_fit_elapsed_s <= 15.0 + 1e-9
        )
        stable = self.previous is not None and consecutive and all(
            abs(current[key] - self.previous[key]) / abs(self.previous[key]) < self.threshold
            for key in self.keys
        )
        self.stable_updates = self.stable_updates + 1 if stable else 0
        self.previous = current
        self.last_fit_elapsed_s = elapsed_s
        return self.stable_updates >= self.required_updates


def growth_in_band(values: Mapping[str, Any], target: float = 3e-8) -> bool:
    calculated = values.get("G_model")
    measured = values.get("G_measure_KF")
    if calculated is None or measured is None:
        return False
    a, b = float(calculated), float(measured)
    return all(math.isfinite(v) for v in (a, b)) and max(
        abs(a - target), abs(b - target), abs(a - b),
    ) <= 0.20 * target


def jacket_at_limit(values: Mapping[str, Any], lower_K: float) -> bool:
    setpoint, actual = values.get("T_j_set"), values.get("T_j")
    return setpoint is not None and actual is not None and (
        lower_K - 1e-6 <= float(setpoint) <= lower_K + 0.01
        and lower_K - 1e-6 <= float(actual) <= lower_K + 0.5
    )


def jacket_constrained(values: Mapping[str, Any], lower_K: float) -> bool:
    """Command saturation precedes the lagging jacket reaching the endpoint."""
    setpoint = values.get("T_j_set")
    return setpoint is not None and lower_K - 1e-6 <= float(setpoint) <= lower_K + 0.01


class AcceptanceWriter:
    """Native measurement writer with a collision-proof acceptance-only tag.

    The normal measurement name/fields and actual computed_at timestamp are
    untouched, so the existing default Grafana dashboard can read the run.
    """
    def __init__(self, writer: Any) -> None:
        self.writer = writer
        self.point_count = 0
        self.tick_ids: set[int] = set()

    def write_tagged_fields(self, fields: dict[str, Any], *, tags=None,
                            measurement: str, timestamp=None) -> None:
        tick = int(fields["tick_seq"])
        if tick in self.tick_ids:
            raise ValueError("An acceptance tick must be written only once.")
        actual_tags = dict(tags or {})
        actual_tags.update({"acceptance_tick": str(tick), "acceptance_source": "accelerated_numerical"})
        self.writer.write_tagged_fields(fields, tags=actual_tags,
                                       measurement=measurement, timestamp=timestamp)
        self.tick_ids.add(tick)
        self.point_count += 1

    def close(self) -> None:
        self.writer.close()


class RuntimeAnnotationWriter:
    """Enrich existing runtime annotations without changing their apply time."""
    def __init__(self, writer: Any, elapsed_s: float, phase: int) -> None:
        self.writer, self.elapsed_s, self.phase = writer, elapsed_s, phase

    def write_tagged_fields(self, fields: dict[str, Any], **kwargs) -> None:
        actual = dict(fields)
        actual.update({"acceptance_source": "accelerated_numerical", "simulation_minute": self.elapsed_s / 60,
                       "phase": self.phase})
        actual["text"] += f"; accelerated numerical simulation; sim minute {self.elapsed_s / 60:.2f}"
        self.writer.write_tagged_fields(actual, **kwargs)


@dataclass
class FourPhaseGates:
    phase: int = 1
    onset: ContinuousGate = field(default_factory=ContinuousGate)
    growth: ContinuousGate = field(default_factory=ContinuousGate)
    endpoint: ContinuousGate = field(default_factory=ContinuousGate)
    loss: ContinuousGate = field(default_factory=ContinuousGate)
    ea_fits: SuccessfulFitGate = field(default_factory=lambda: SuccessfulFitGate(("E_A",)))
    all_fits: SuccessfulFitGate = field(default_factory=lambda: SuccessfulFitGate(("E_A", "k_0", "n")))
    initial_k_0: float = 2e15
    initial_n: float = 1.75
    tracking_failure: bool = False

    def observe(self, *, result: Mapping[str, Any], fitting: Mapping[str, Any],
                elapsed_s: float, jacket_min_K: float, seed_consumed: bool) -> str | None:
        """Return transition/terminal action; never advance an unmet stage."""
        near_limit = jacket_at_limit(result, jacket_min_K)
        if self.phase < 4 and self.endpoint.observe(near_limit, elapsed_s):
            return "premature_jacket_limit"
        if self.phase == 1:
            sigma = result.get("sigma")
            ready = sigma is not None and float(sigma) > 0 and abs(float(sigma) - 0.035) <= 0.005
            if self.onset.observe(ready, elapsed_s):
                self.phase = 2
                return "seed_and_E_A"
        elif self.phase == 2:
            if not math.isclose(float(result["k_0"]), self.initial_k_0, rel_tol=1e-12) or not math.isclose(float(result["n"]), self.initial_n, rel_tol=1e-12):
                return "phase2_fixed_parameters_changed"
            if seed_consumed and self.ea_fits.observe(int(fitting.get("fit_count", 0)), result, elapsed_s):
                self.phase = 3
                self.all_fits.last_fit_count = int(fitting.get("fit_count", 0))
                return "G_and_all"
        elif self.phase == 3:
            fits_ready = self.all_fits.observe(int(fitting.get("fit_count", 0)), result, elapsed_s)
            growth_ready = self.growth.observe(growth_in_band(result), elapsed_s)
            if fits_ready and growth_ready:
                self.phase = 4
                return "steady_control"
        else:
            constrained = jacket_constrained(result, jacket_min_K)
            if not constrained and self.loss.observe(not growth_in_band(result), elapsed_s):
                self.tracking_failure = True
            if constrained:
                self.loss.observe(False, elapsed_s)
            if self.endpoint.observe(near_limit, elapsed_s):
                return "jacket_limit"
        return None


class FourPhaseAcceptance:
    """One isolated service instance, ordinary command API, genuine computed data."""
    def __init__(self, settings: ControllerSettings, directory: Path, *,
                 run_id: str | None = None, writer: Any | None = None,
                 max_minutes: float = 120.0, progress_every: int = 20) -> None:
        if settings.process_enabled or settings.process_write_enabled:
            raise ValueError("Acceptance must never enable physical OPC UA access.")
        if directory.exists():
            raise ValueError("Acceptance output directory must be new; no overwrite or cleanup.")
        if not 0 < max_minutes <= 120:
            raise ValueError("max_minutes must be positive and no greater than 120.")
        self.directory = directory
        self.run_id = run_id or "exp_refresh_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
        self.clock = 0.0
        self.params = acceptance_parameters()
        self.writer = AcceptanceWriter(writer) if writer is not None else None
        self.service = ControllerService(settings=settings, adapter=CrystallizationControllerAdapter(),
                                         measurement_writer=self.writer, monotonic_clock=lambda: self.clock)
        self.gates = FourPhaseGates()
        self.max_ticks = int(max_minutes * 60 / float(self.params["dt"]))
        self.progress_every = progress_every
        self.events: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.phase_results = {str(i): {"name": name, "status": "NOT RUN"} for i, name in PHASE_NAMES.items()}
        self.sequence = 0
        self.seed_id: str | None = None
        self.seed_consumed_tick: int | None = None
        self.pending_event_id: str | None = None
        self.expected_refresh_count = 0
        self.seen_refresh_count = 0
        self.start_wall = utc_ts()
        self.latest_computed_at: str | None = None
        self.first_tick_annotated = False
        self.annotation_count = 0

    def _event(self, name: str, *, actual: bool = False, wall_utc: str | None = None, **extra) -> None:
        event = {"name": name, "phase": self.gates.phase,
                            "tick_seq": self.service.control_tick_count,
                            "elapsed_s": self.clock, "simulation_minute": self.clock / 60,
                            "wall_utc": wall_utc or utc_ts(), "actual_tick_event": actual, **extra}
        self.events.append(event)
        if actual and self.writer is not None:
            if event["tick_seq"] <= 0:
                raise RuntimeError("An executed-tick annotation cannot use a request or startup timestamp.")
            if name == "runtime_applied":
                event_id = extra["receipt"]["event_id"]
                entry = self.service.runtime_events[event_id]
                write_runtime_event(RuntimeAnnotationWriter(self.writer.writer, self.clock, self.gates.phase), entry)
            else:
                text = (f"{name}; phase {self.gates.phase} {PHASE_NAMES[self.gates.phase]}; "
                        f"effective tick {event['tick_seq']}; sim minute {self.clock / 60:.2f}; "
                        "accelerated numerical simulation / simulated growth")
                self.writer.writer.write_tagged_fields(
                    {"title": name.replace("_", " "), "text": text, "event_name": name,
                     "effective_tick": event["tick_seq"], "elapsed_s": self.clock,
                     "simulation_minute": self.clock / 60, "phase": self.gates.phase,
                     "applied_at": event["wall_utc"]},
                    tags={"run_id": self.run_id, "event_id": f"acceptance:{name}:{event['tick_seq']}"},
                    measurement=RUNTIME_EVENT_MEASUREMENT,
                    timestamp=datetime.fromisoformat(event["wall_utc"].replace("Z", "+00:00")),
                )
            self.annotation_count += 1

    def _command(self, name: str, payload: Mapping[str, Any], *, msg_type="command") -> dict[str, Any]:
        self.sequence += 1
        message = build_envelope("central", "controller", msg_type, name,
                                 self.sequence, dict(payload))
        result = self.service.on_message(message)
        self._event("command", command=name, payload=dict(payload), receipt=copy.deepcopy(result))
        if not result.get("accepted"):
            raise RuntimeError(f"{name} rejected: {result.get('reason', 'unknown error')}")
        return result

    def _runtime(self, changes: Mapping[str, Any]) -> None:
        event_id = uuid4().hex
        self._command(CONTROLLER_RUNTIME_UPDATE_COMMAND, {
            "run_id": self.run_id, "event_id": event_id,
            "expected_revision": self.service.runtime_revision,
            "changes": dict(changes), "requested_at": utc_ts(),
        })
        self.pending_event_id = event_id
        self.expected_refresh_count += 1

    def _capture(self) -> tuple[dict[str, Any], dict[str, Any]]:
        status = self.service.status()
        output = status.get("last_control_output")
        # This key is present in status on the current service; direct access
        # also works with a status endpoint that deliberately omits large data.
        if output is None:
            output = self.service.last_control_output
        if output is None or int(output["tick_seq"]) != self.service.control_tick_count:
            raise RuntimeError("Every executed acceptance tick needs a new actual result.")
        result = output["result"]
        self.latest_computed_at = output["computed_at"]
        if not result.get("valid"):
            raise RuntimeError(f"Invalid control output: {result.get('error')}")
        if self.writer is not None and not output.get("influxdb_written"):
            raise RuntimeError("A Controller measurement was not persisted to InfluxDB.")
        fitting = status["adaptation"].get("fitting") or {}
        if not self.first_tick_annotated:
            self._mark_phase(1, "RUNNING", start_tick=self.service.control_tick_count)
            self._event("phase_started", actual=True, wall_utc=output["computed_at"], name_in_paper=PHASE_NAMES[1])
            self.first_tick_annotated = True
        refresh = status["runtime_controls"].get("refresh") or {}
        refresh_count = int(refresh.get("count", 0))
        if refresh_count > self.seen_refresh_count:
            if refresh_count != self.seen_refresh_count + 1:
                raise RuntimeError("One acceptance tick may not perform multiple Controller refreshes.")
            self._event("refresh_completed", actual=True, wall_utc=output["computed_at"], refresh=copy.deepcopy(refresh))
            self.seen_refresh_count = refresh_count
        if self.pending_event_id:
            last_result = status["runtime_controls"].get("last_result") or {}
            if last_result.get("event_id") == self.pending_event_id:
                if last_result.get("status") == "applied":
                    if refresh_count != self.expected_refresh_count:
                        raise RuntimeError("One changed runtime batch must cause exactly one Controller refresh.")
                    if last_result.get("effective_tick") != self.service.control_tick_count:
                        raise RuntimeError("Runtime acknowledgement must name the actual refresh tick.")
                    self._event("runtime_applied", actual=True, wall_utc=last_result["applied_at"], receipt=copy.deepcopy(last_result))
                    self._mark_phase(self.gates.phase, "RUNNING", start_tick=self.service.control_tick_count)
                    self._event("phase_started", actual=True, wall_utc=last_result["applied_at"], name_in_paper=PHASE_NAMES[self.gates.phase])
                    self.pending_event_id = None
                elif last_result.get("status") in {"rejected", "failed"}:
                    raise RuntimeError(f"Runtime refresh failed: {last_result}")
        controller = self.service.adapter.controller
        if self.seed_id and self.seed_consumed_tick is None and not controller.pending_seed:
            if not controller.mark_seed or not (controller.size_list > 0).any():
                raise RuntimeError("Seed event was recorded but not consumed by the numerical process.")
            seeds = status["seed_events"]
            if seeds["count"] != 1 or seeds["last"]["event_id"] != self.seed_id:
                raise RuntimeError("Exactly one matching seed event is required.")
            self.seed_consumed_tick = self.service.control_tick_count
            self._event("seed_consumed", actual=True, wall_utc=output["computed_at"], event_id=self.seed_id, mass_kg=self.params["m_seed"])
        row = {"run_id": self.run_id, "phase": self.gates.phase,
               "tick_seq": output["tick_seq"], "elapsed_s": output["elapsed_s"],
               "simulation_minute": float(output["elapsed_s"]) / 60,
               "computed_at": output["computed_at"], "runtime_revision": output["runtime_revision"],
               "refresh_count": refresh_count, "fitting_segment_id": refresh.get("segment_id"),
               "fit_count": fitting.get("fit_count", 0), "fit_failure_count": fitting.get("failure_count", 0),
               "fit_sample_count": fitting.get("sample_count", 0), "fit_status": fitting.get("last_status"),
               "seed_event_count": status["seed_events"]["count"],
               "seed_consumed_tick": self.seed_consumed_tick,
               **copy.deepcopy(result)}
        # The service captures only the completed tick, after runtime commit.
        # Full initialization matrices belong in raw JSONL, not reconstructed
        # later from a checkpoint or mutable algorithm/filter objects.
        snapshot = self.service._last_committed_diagnostics
        diagnostic = None
        diagnostic_error = self.service._last_diagnostics_capture_error
        if snapshot is not None:
            try:
                diagnostic = snapshot.to_dict()
                if (diagnostic["run_id"] != self.run_id
                        or diagnostic["tick_seq"] != row["tick_seq"]
                        or diagnostic["elapsed_s"] != row["elapsed_s"]):
                    raise ValueError("Diagnostic snapshot is not this tick.")
                json.dumps(diagnostic, allow_nan=False)
            except Exception:
                diagnostic = None
                diagnostic_error = "invalid_snapshot"
        row.update(diagnostics=diagnostic, diag_capture_ok=diagnostic is not None,
                   diag_capture_error=diagnostic_error)
        self.rows.append(row)
        return result, fitting

    def _mark_phase(self, phase: int, state: str, **extra) -> None:
        self.phase_results[str(phase)].update({"status": state, **extra})

    def run(self) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=False)
        report: dict[str, Any]
        reason = "simulation_time_limit"
        failure = None
        self._mark_phase(1, "RUNNING", start_tick=0)
        try:
            self.service.start(scheduler_enabled=False, consumer_enabled=False)
            self._command(PARAMS_UPDATE_MESSAGE, {"version": 1, "params": self.params}, msg_type="params")
            self._command(EXPERIMENT_START_COMMAND, {
                "run_id": self.run_id, "parameter_version": 1,
                "started_at": self.start_wall, "adaptation_enabled": False,
                "adaptation_mode": "E_A",
            })
            self._event("phase_change_requested", name_in_paper=PHASE_NAMES[1])
            with (self.directory / "ticks.jsonl").open("x", encoding="utf-8") as ticks_file:
                for tick in range(1, self.max_ticks + 1):
                    self.clock = tick * float(self.params["dt"])
                    tick_receipt = self.service._control_tick_once(now=self.clock)
                    if not tick_receipt.get("executed") or self.service.status()["scheduler"]["thread_alive"]:
                        raise RuntimeError("Acceptance tick execution must be manual, not duplicated by a background scheduler.")
                    result, fitting = self._capture()
                    ticks_file.write(json.dumps(self.rows[-1], allow_nan=False) + "\n")
                    ticks_file.flush()
                    if not float(self.params["T_j_min"]) - 1e-6 <= float(result["T_j_set"]) <= float(self.params["T_j_max"]) + 1e-6:
                        raise RuntimeError("Jacket command exceeded the unchanged configured actuator bounds.")
                    action = None if self.pending_event_id else self.gates.observe(
                        result=result, fitting=fitting, elapsed_s=self.clock,
                        jacket_min_K=float(self.params["T_j_min"]), seed_consumed=self.seed_consumed_tick is not None,
                    )
                    if action == "seed_and_E_A":
                        self._mark_phase(1, "PASS", end_tick=tick)
                        self._mark_phase(2, "RUNNING", requested_tick=tick)
                        self.seed_id = uuid4().hex
                        self._command(CONTROLLER_ADD_SEED_COMMAND, {
                            "run_id": self.run_id, "event_id": self.seed_id, "added_at": utc_ts(),
                        })
                        self._runtime({"adaptation_enabled": True, "adaptation_mode": "E_A"})
                        self._event("phase_change_requested", name_in_paper=PHASE_NAMES[2])
                    elif action == "G_and_all":
                        self._mark_phase(2, "PASS", end_tick=tick)
                        self._mark_phase(3, "RUNNING", requested_tick=tick)
                        self._runtime({"control_target": "G", "adaptation_mode": "all"})
                        self._event("phase_change_requested", name_in_paper=PHASE_NAMES[3])
                    elif action == "steady_control":
                        self._mark_phase(3, "PASS", end_tick=tick)
                        self._mark_phase(4, "RUNNING", start_tick=tick)
                        self._event("phase_started", actual=True, wall_utc=self.latest_computed_at, name_in_paper=PHASE_NAMES[4])
                    elif action == "jacket_limit":
                        reason = action
                        self._mark_phase(4, "FAIL" if self.gates.tracking_failure else "PASS", end_tick=tick)
                        self._event("endpoint_reached", actual=True, wall_utc=self.latest_computed_at, lower_K=self.params["T_j_min"])
                        break
                    elif action:
                        reason = action
                        raise RuntimeError(action)
                    if self.progress_every and tick % self.progress_every == 0:
                        print(f"tick={tick} sim_min={self.clock / 60:.2f} phase={self.gates.phase} "
                              f"fits={fitting.get('fit_count', 0)} samples={fitting.get('sample_count', 0)} "
                              f"sigma={float(result['sigma']):.5f} Tj={float(result['T_j']) - 273.15:.2f}C", flush=True)
        except KeyboardInterrupt:
            reason = "operator_interrupted"
            failure = "Operator interrupted the numerical acceptance run."
        except Exception as exc:
            reason = "execution_error" if reason == "simulation_time_limit" else reason
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            if self.service.status()["active"]:
                try:
                    self._command(EXPERIMENT_STOP_COMMAND, {
                        "run_id": self.run_id, "stopped_at": utc_ts(), "reason": reason,
                    })
                except Exception as exc:
                    failure = failure or f"Failed to stop this acceptance experiment: {exc}"
            self.service.stop()
            if self.gates.tracking_failure:
                failure = failure or "Growth tracking was lost for 60 simulation seconds before the jacket constraint."
            for value in self.phase_results.values():
                if value["status"] == "RUNNING":
                    value["status"] = "FAIL" if failure else "INCOMPLETE"
                    value["end_tick"] = self.service.control_tick_count
            if self.writer is None:
                failure = failure or "No InfluxDB writer: this is a local smoke run, not complete Grafana acceptance."
            if self.seen_refresh_count != self.expected_refresh_count:
                failure = failure or "Refresh count does not match completed changed runtime batches."
            if self.seed_consumed_tick is None:
                failure = failure or "The single seed event did not reach the numerical process."
            report = self._report(reason, failure)
            self._save(report)
        return report

    def _report(self, reason: str, failure: str | None) -> dict[str, Any]:
        end_wall = utc_ts()
        passed = failure is None and reason == "jacket_limit" and all(v["status"] == "PASS" for v in self.phase_results.values())
        times = [row["computed_at"] for row in self.rows]
        from_ms = int((datetime.fromisoformat(times[0].replace("Z", "+00:00")) - timedelta(seconds=5)).timestamp() * 1000) if times else 0
        to_ms = int((datetime.fromisoformat(times[-1].replace("Z", "+00:00")) + timedelta(seconds=5)).timestamp() * 1000) if times else 0
        native_url = "http://127.0.0.1:3000/d/crystallization-mpc-analysis/crystallization-mpc-analysis?" + urlencode({
            "var-run_id": self.run_id, "var-runtime_annotations": "true", "from": from_ms, "to": to_ms,
        })
        errors = [abs(row["G_model"] - self.params["G_set"]) for row in self.rows if row["phase"] == 4 and row.get("G_model") is not None]
        measured_errors = [abs(row["G_measure_KF"] - self.params["G_set"]) for row in self.rows if row["phase"] == 4 and row.get("G_measure_KF") is not None]
        pair_errors = [abs(row["G_model"] - row["G_measure_KF"]) for row in self.rows if row["phase"] == 4 and row.get("G_model") is not None and row.get("G_measure_KF") is not None]
        return {
            "run_id": self.run_id, "status": "PASS" if passed else "FAIL",
            "endpoint_reason": reason, "failure": failure,
            "wall_started_at": self.start_wall, "wall_finished_at": end_wall,
            "simulation_elapsed_s": self.clock, "simulation_minutes": self.clock / 60,
            "tick_count": len(self.rows), "influx_point_count": self.writer.point_count if self.writer else 0,
            "influx_annotation_count": self.annotation_count,
            "source": "accelerated numerical simulation / simulated growth",
            "command_transport": "validated ControllerService.on_message envelopes; no camera or physical hardware",
            "timing": "computed_at is actual UTC; tick_seq * 15 seconds is simulation time; use timeline.csv for the default dashboard axis mapping",
            "default_grafana_url": native_url, "phases": self.phase_results,
            "seed_events": self.service.status()["seed_events"], "seed_consumed_tick": self.seed_consumed_tick,
            "refresh_count": self.seen_refresh_count, "expected_refresh_count": self.expected_refresh_count,
            "phase4_model_target_error_max_m_s": max(errors) if errors else None,
            "phase4_measured_target_error_max_m_s": max(measured_errors) if measured_errors else None,
            "phase4_model_measurement_error_max_m_s": max(pair_errors) if pair_errors else None,
            "criteria": {
                "sigma_absolute_tolerance": 0.005, "stable_seconds": 60,
                "successful_stable_parameter_updates": 4, "parameter_relative_change_limit": 0.10,
                "growth_absolute_tolerance_m_s": 6e-9,
                "jacket_setpoint_tolerance_K": 0.01, "jacket_actual_tolerance_K": 0.5,
                "hard_limit_simulation_minutes": 120,
            },
            "not_run": {name: {"status": "NOT RUN", "reason": explanation} for name, explanation in NOT_RUN.items()},
            "screenshots": "Use only the existing default Grafana dashboard; browser screenshots are a separate delivery step, never generated replacement plots.",
            "diagnostic_capture": self._diagnostic_capture_report(),
            "refresh_verification": self._refresh_diagnostics_report(),
            "control_performance": {
                "status": "FAIL" if self.gates.tracking_failure else (
                    "PASS" if reason == "jacket_limit" and passed else "INCOMPLETE"
                ),
                "tracking_failure": self.gates.tracking_failure,
                "endpoint_status": "PASS" if reason == "jacket_limit" else "INCOMPLETE",
                "note": "Diagnostic evidence never overrides control tracking or endpoint acceptance.",
            },
        }

    def _diagnostic_capture_report(self) -> dict[str, Any]:
        captured = [row for row in self.rows if row.get("diag_capture_ok")]
        return {
            "status": "PASS" if self.rows and len(captured) == len(self.rows) else "FAIL",
            "captured_ticks": len(captured), "expected_ticks": len(self.rows),
            "missing_ticks": [row["tick_seq"] for row in self.rows if not row.get("diag_capture_ok")],
            "note": "Collection only; rendered dashboard and hardware acceptance are separate checks.",
        }

    def _refresh_diagnostics_report(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        expected_filters = {"main": 4, "G": 3, "E_A": 3, "k_0": 3, "n": 3}
        previous_count = 0
        for row in self.rows:
            current_count = int(row.get("refresh_count", 0))
            if current_count <= previous_count:
                continue
            previous_count = current_count
            failed: list[str] = []
            document = row.get("diagnostics") or {}
            scalars = document.get("fields") or {}
            raw = document.get("raw") or {}
            refresh = raw.get("refresh_init") or {}
            filters = raw.get("filters") or {}
            if set(refresh) != set(expected_filters):
                failed.append("Missing full initialization snapshots for one or more filters.")
            expected_main = [row.get("T"), 0.0, row.get("c"), 0.0]
            if refresh.get("main", {}).get("state") != expected_main:
                failed.append("Main EKF did not initialize from current T/c with zero derivatives.")
            if (scalars.get("diag_snapshot_stage") != "refresh_init"
                    or scalars.get("diag_refresh_applied") is not True
                    or scalars.get("diag_refresh_count") != current_count):
                failed.append("Diagnostic lifecycle did not match the committed refresh tick.")
            for name, size in expected_filters.items():
                item = refresh.get(name) or {}
                if name != "main" and item.get("state") != [0.0] * size:
                    failed.append(f"{name}: initialization state did not match constructor zeros.")
                covariance = item.get("covariance")
                initial = (filters.get(name) or {}).get("initial_covariance")
                valid_shape = (isinstance(covariance, list) and len(covariance) == size
                               and all(isinstance(line, list) and len(line) == size for line in covariance))
                if not valid_shape or initial is None or covariance != initial:
                    failed.append(f"{name}: full covariance did not equal its constructor initial matrix.")
            if row.get("fit_sample_count") != 0:
                failed.append("New fitting segment did not start with zero samples.")
            if row.get("dT_dt_KF") != 0.0 or row.get("dc_dt_KF") != 0.0:
                failed.append("Refresh tick derivative outputs were not zero.")
            if any(scalars.get(name) != 0.0 for name in
                   ("diag_int_target", "diag_int_dT_dt", "diag_int_T")):
                failed.append("Refresh tick control integrals were not zero.")
            next_row = next((item for item in self.rows
                             if item["tick_seq"] == row["tick_seq"] + 1), None)
            if next_row is None:
                failed.append("No next-cycle evidence of filter recovery was collected.")
            else:
                next_document = next_row.get("diagnostics") or {}
                next_fields = next_document.get("fields") or {}
                if (next_fields.get("diag_main_lifecycle") != "updated"
                        or next_fields.get("diag_refresh_applied") is not False):
                    failed.append("Main EKF did not resume normal updates next cycle.")
                if next_fields.get("diag_G_lifecycle") not in {"updated", "reanchored_updated", "paused"}:
                    failed.append("G filter had no valid next-cycle update/pause lifecycle.")
            checks.append({"tick_seq": row["tick_seq"], "refresh_count": current_count,
                           "status": "FAIL" if failed else "PASS", "failures": failed})
        expected = self.expected_refresh_count
        status = ("NOT RUN" if expected == 0 and not checks else
                  "PASS" if len(checks) == expected and all(c["status"] == "PASS" for c in checks) else "FAIL")
        return {"status": status, "expected_refreshes": expected,
                "verified_refreshes": len(checks), "checks": checks,
                "note": "Full initialization state/P verified from immutable raw snapshots; no claim of performance improvement."}

    def _save(self, report: dict[str, Any]) -> None:
        documents = {"report.json": report, "parameters.json": self.params,
                     "events.json": self.events, "final_status.json": self.service.status()}
        for name, document in documents.items():
            (self.directory / name).write_text(json.dumps(document, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        with (self.directory / "events.csv").open("x", newline="", encoding="utf-8") as stream:
            fields = ["name", "phase", "tick_seq", "simulation_minute", "wall_utc", "details_json"]
            event_writer = csv.DictWriter(stream, fieldnames=fields)
            event_writer.writeheader()
            for event in self.events:
                event_writer.writerow({**{key: event[key] for key in fields[:-1]},
                                       "details_json": json.dumps({key: value for key, value in event.items() if key not in fields}, allow_nan=False)})
        if self.rows:
            with (self.directory / "ticks.csv").open("x", newline="", encoding="utf-8") as stream:
                csv_writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
                csv_writer.writeheader()
                csv_writer.writerows({**row, "diagnostics": json.dumps(row["diagnostics"], allow_nan=False)}
                                     for row in self.rows)
            with (self.directory / "timeline.csv").open("x", newline="", encoding="utf-8") as stream:
                fields = ["tick_seq", "simulation_minute", "computed_at", "phase", "runtime_revision", "refresh_count", "fit_sample_count"]
                csv_writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                csv_writer.writeheader()
                csv_writer.writerows(self.rows)
        text = [f"# {self.run_id}", "", f"Result: {report['status']}; endpoint: {report['endpoint_reason']}",
                f"Simulation time: {self.clock / 60:.2f} min. Actual UTC: {self.start_wall} to {report['wall_finished_at']}.",
                "", "This is accelerated numerical simulation, not physical or image-based acceptance.", "",
                f"Default Grafana dashboard: {report['default_grafana_url']}", "",
                "The default dashboard keeps its UTC axis. timeline.csv maps every actual timestamp to simulation minutes.", "",
                "| Phase | Result |", "|---|---|"]
        text.extend(f"| {value['name']} | {value['status']} |" for value in self.phase_results.values())
        text += ["", f"Failure/incomplete reason: {report['failure'] or report['endpoint_reason']}", "",
                 f"Diagnostic collection: {report['diagnostic_capture']['status']}; "
                 f"refresh initialization verification: {report['refresh_verification']['status']}.",
                 f"Control tracking: {report['control_performance']['status']}; "
                 f"endpoint: {report['control_performance']['endpoint_status']}.", "",
                 "Diagnostic PASS does not override a control FAIL. ticks.jsonl retains full initialization state and covariance matrices.", "",
                 "No stage was forced, no process kinetics or actuator bounds were tuned, and no old run was deleted.", "",
                 "NOT RUN: camera warmup/latency, FBRM, physical seed dissolution, nucleation/full size-distribution control.", ""]
        (self.directory / "report.md").write_text("\n".join(text), encoding="utf-8")


__all__ = ["AcceptanceWriter", "ContinuousGate", "FourPhaseAcceptance", "FourPhaseGates",
           "SuccessfulFitGate", "acceptance_parameters", "growth_in_band", "jacket_at_limit", "jacket_constrained"]
