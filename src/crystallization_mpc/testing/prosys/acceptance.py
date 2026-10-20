"""Pure, real-time gates for the paper's external Prosys experiment.

This module does not drive a Controller, provide measurements, or prove an OPC
UA connection. Its actions must be acknowledged by the external driver before
another observation is submitted. Integration and refresh evidence are separate
from numerical stage eligibility. The historical accelerated acceptance profile
is deliberately not imported or changed.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
from typing import Any, Mapping


PHASE_NAMES = {
    1: "Onset Preparation", 2: "Preliminary Adjustment",
    3: "Status Adjustment", 4: "Control Operation",
}
REQUIRED_CHECKS = {
    "integration": (
        "opcua_read", "opcua_write_readback", "process_response", "failure_recovery",
    ),
    "filter_refresh": (
        "refresh_state", "refresh_covariance", "refresh_integrals",
        "refresh_fit_segment", "refresh_resume",
    ),
    "procedure_actions": (
        "seed_once_1g", "phase2_E_A_only", "phase3_atomic_G_all",
    ),
}
CHECK_STATUSES = {"PASS", "FAIL", "NOT_RUN", "INCOMPLETE"}


def acceptance_parameters() -> dict[str, Any]:
    """Overrides for the ordinary parameter builder, not a second plant model."""
    return {
        "mode": "MPC", "run_type": "experiment",
        "growth_rate_source": "live_gsensor", "target": "sigma",
        "sigma_set": 0.035, "G_set": 3e-8, "dt": 15.0, "dt_G": 15.0,
        "E_A": 150000.0, "k_0": 2e15, "n": 1.75, "m_seed": 0.001,
    }


def _finite(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class AcceptanceRow:
    """One unique Controller sample; temperatures are K and growth rates m/s.

    ``elapsed_s`` is the actual sample time relative to the experiment's monotonic
    origin, not the time of a later HTTP poll. ``seed_count`` and
    ``seed_consumed`` come from the independent plant's event acknowledgement.
    Missing/stale/invalid input must set ``valid=False``; it is never a zero.
    """
    tick: int
    elapsed_s: float
    sigma: float | None = None
    G_model: float | None = None
    G_measure_KF: float | None = None
    E_A: float | None = None
    k_0: float | None = None
    n: float | None = None
    fit_count: int = 0
    fit_status: str = "not_run"
    T_j: float | None = None
    T_j_set: float | None = None
    seed_count: int = 0
    seed_consumed: bool = False
    valid: bool = True

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "AcceptanceRow":
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in row.items() if key in names})


@dataclass
class DwellGate:
    duration_s: float = 60.0
    since: float | None = None

    def reset(self) -> None:
        self.since = None

    def observe(self, satisfied: bool, elapsed_s: float) -> bool:
        if not satisfied:
            self.reset()
            return False
        if self.since is None:
            self.since = elapsed_s
        return elapsed_s - self.since >= self.duration_s


@dataclass
class FitStabilityGate:
    keys: tuple[str, ...]
    relative_limit: float = 0.10
    required_changes: int = 4
    previous: dict[str, float] | None = None
    last_fit_count: int | None = None
    stable_changes: int = 0

    def reset(self) -> None:
        self.previous = None
        self.last_fit_count = None
        self.stable_changes = 0

    def observe(self, row: AcceptanceRow) -> bool:
        values = {key: getattr(row, key) for key in self.keys}
        if (
            row.fit_status != "fit_succeeded"
            or not isinstance(row.fit_count, int) or isinstance(row.fit_count, bool)
            or row.fit_count <= 0
            or any(not _finite(value) or value <= 0 for value in values.values())
        ):
            self.reset()
            return False
        if self.last_fit_count is not None and row.fit_count <= self.last_fit_count:
            # A flat parameter without a *new* successful fit is not stability.
            self.reset()
            return False
        consecutive_fit = self.last_fit_count is not None and row.fit_count == self.last_fit_count + 1
        stable = self.previous is not None and consecutive_fit and all(
            abs(values[key] - self.previous[key]) / abs(self.previous[key])
            <= self.relative_limit + 1e-12 for key in self.keys
        )
        self.stable_changes = self.stable_changes + 1 if stable else 0
        self.previous = values
        self.last_fit_count = row.fit_count
        return self.stable_changes >= self.required_changes


class PaperAcceptance:
    """Paper stage state machine, requiring separate evidence for PASS claims.

    Polling duplicates never contribute to dwell. A skipped Controller tick,
    invalid sample, non-increasing timestamp, or gap exceeding ``max_gap_s``
    breaks continuity. Thirty seconds allows normal scheduling jitter around a
    15-second control period without treating a stalled process as continuous.

    The caller must stop on a terminal action. On transition actions it must
    synchronously apply/verify the requested change; call ``fail`` if that fails.
    Call ``check_timeout`` even when no new measurements are arriving.
    """
    def __init__(self, *, max_elapsed_s: float = 6 * 3600, max_gap_s: float = 30.0) -> None:
        if not _finite(max_elapsed_s) or not 0 < max_elapsed_s <= 6 * 3600:
            raise ValueError("The paper run limit must be positive and at most six real hours.")
        if not _finite(max_gap_s) or max_gap_s <= 15:
            raise ValueError("max_gap_s must allow jitter beyond the 15-second sampling period.")
        self.max_elapsed_s, self.max_gap_s = float(max_elapsed_s), float(max_gap_s)
        self.phase = 1
        self.onset = DwellGate()
        self.growth = DwellGate()
        self.endpoint = DwellGate()
        self.ea_fits = FitStabilityGate(("E_A",))
        self.all_fits = FitStabilityGate(("E_A", "k_0", "n"))
        self.last_row: AcceptanceRow | None = None
        self.terminal_reason: str | None = None
        self.failure_reason: str | None = None
        self.endpoint_elapsed_s: float | None = None
        self.observed_elapsed_s = 0.0
        self.unique_samples = 0
        self.duplicates = 0
        self.invalid_samples = 0
        self.continuity_breaks = 0
        self.seed_seen = False
        self.transitions: list[dict[str, Any]] = []
        self.control_samples: list[tuple[AcceptanceRow, bool]] = []
        self.checks = {
            category: {name: {"status": "NOT_RUN", "detail": "No evidence recorded."}
                       for name in names}
            for category, names in REQUIRED_CHECKS.items()
        }

    def record_check(self, category: str, name: str, status: str, detail: str = "") -> None:
        if category not in self.checks or name not in self.checks[category]:
            raise ValueError("Record a named required check; do not implicitly waive missing evidence.")
        if status not in CHECK_STATUSES:
            raise ValueError(f"Unsupported evidence status: {status}")
        previous = self.checks[category][name]
        if previous["status"] == "FAIL" and status != "FAIL":
            raise ValueError("A failed check cannot be overwritten in the same experiment.")
        self.checks[category][name] = {"status": status, "detail": str(detail)}

    def fail(self, reason: str) -> str:
        if not reason:
            raise ValueError("A failure reason is required.")
        self.failure_reason = self.failure_reason or str(reason)
        self.terminal_reason = self.failure_reason
        return self.terminal_reason

    def check_timeout(self, elapsed_s: float) -> str | None:
        """Use the real monotonic clock, including while a server is silent."""
        if not _finite(elapsed_s) or elapsed_s < 0:
            raise ValueError("Elapsed time must be finite and nonnegative.")
        self.observed_elapsed_s = max(self.observed_elapsed_s, elapsed_s)
        if self.terminal_reason is None and elapsed_s >= self.max_elapsed_s:
            self.terminal_reason = "timeout"
        return self.terminal_reason

    @staticmethod
    def growth_in_band(row: AcceptanceRow) -> bool:
        a, b = row.G_model, row.G_measure_KF
        return _finite(a) and _finite(b) and max(
            abs(a - 3e-8), abs(b - 3e-8), abs(a - b),
        ) <= 6e-9 + 1e-20

    @staticmethod
    def jacket_constrained(row: AcceptanceRow) -> bool:
        return _finite(row.T_j_set) and 253.15 - 1e-6 <= row.T_j_set <= 253.16 + 1e-10

    @classmethod
    def jacket_at_limit(cls, row: AcceptanceRow) -> bool:
        return cls.jacket_constrained(row) and _finite(row.T_j) and (
            253.15 - 1e-6 <= row.T_j <= 253.65 + 1e-10
        )

    def _reset_gates(self) -> None:
        for gate in (self.onset, self.growth, self.endpoint, self.ea_fits, self.all_fits):
            gate.reset()

    def _transition(self, action: str, row: AcceptanceRow) -> str:
        self.transitions.append({
            "from_phase": self.phase, "to_phase": self.phase + 1,
            "tick": row.tick, "elapsed_s": row.elapsed_s, "action": action,
        })
        self.phase += 1
        self._reset_gates()
        return action

    def observe(self, row: AcceptanceRow | Mapping[str, Any]) -> str | None:
        if not isinstance(row, AcceptanceRow):
            row = AcceptanceRow.from_mapping(row)
        if self.terminal_reason is not None:
            return self.terminal_reason
        if not isinstance(row.tick, int) or isinstance(row.tick, bool) or row.tick < 0:
            raise ValueError("tick must be a nonnegative integer Controller sequence.")
        if not _finite(row.elapsed_s) or row.elapsed_s < 0:
            raise ValueError("Sample elapsed time must be finite and nonnegative.")
        if not isinstance(row.valid, bool) or not isinstance(row.seed_consumed, bool):
            raise ValueError("valid and seed_consumed must be explicit Boolean flags.")
        if row.elapsed_s > self.max_elapsed_s:
            return self.check_timeout(row.elapsed_s)
        self.observed_elapsed_s = max(self.observed_elapsed_s, row.elapsed_s)
        previous = self.last_row
        if previous is not None and row.tick <= previous.tick:
            self.duplicates += 1
            return self.check_timeout(row.elapsed_s)
        continuous = previous is not None and (
            row.tick == previous.tick + 1 and previous.valid and row.valid
            and 0 < row.elapsed_s - previous.elapsed_s <= self.max_gap_s
        )
        if previous is not None and not continuous:
            self.continuity_breaks += 1
            self._reset_gates()
        self.last_row = row
        self.unique_samples += 1
        if not row.valid or (previous is not None and row.elapsed_s <= previous.elapsed_s):
            self.invalid_samples += 1
            self._reset_gates()
            return self.check_timeout(row.elapsed_s)
        if (
            not isinstance(row.seed_count, int) or isinstance(row.seed_count, bool)
            or row.seed_count < 0 or row.seed_count > 1
            or (row.seed_consumed and row.seed_count != 1)
            or (self.seed_seen and (row.seed_count != 1 or not row.seed_consumed))
            or (self.phase == 1 and (row.seed_count != 0 or row.seed_consumed))
        ):
            return self.fail("seed_count_invalid")
        self.seed_seen = self.seed_seen or row.seed_consumed
        if self.phase > 2 and not self.seed_seen:
            return self.fail("seed_acknowledgement_missing")

        near_limit = self.jacket_at_limit(row)
        if self.phase < 4 and self.endpoint.observe(near_limit, row.elapsed_s):
            return self.fail("premature_jacket_limit")
        action = None
        if self.phase == 1:
            ready = _finite(row.sigma) and row.sigma > 0 and abs(row.sigma - 0.035) <= 0.005 + 1e-12
            if self.onset.observe(ready, row.elapsed_s):
                action = self._transition("seed_and_E_A", row)
        elif self.phase == 2:
            if not all(_finite(value) for value in (row.k_0, row.n)):
                self.ea_fits.reset()
            elif not math.isclose(row.k_0, 2e15, rel_tol=1e-12) or not math.isclose(row.n, 1.75, rel_tol=1e-12):
                return self.fail("phase2_fixed_parameters_changed")
            elif row.seed_consumed and self.ea_fits.observe(row):
                action = self._transition("G_and_all", row)
            elif not row.seed_consumed:
                self.ea_fits.reset()
        elif self.phase == 3:
            fits_ready = self.all_fits.observe(row)
            growth_ready = self.growth.observe(self.growth_in_band(row), row.elapsed_s)
            if fits_ready and growth_ready:
                action = self._transition("steady_control", row)
                self.control_samples.append((row, False))
        else:
            # Historical 20%/60s losses are metrics only, never paper failures.
            self.control_samples.append((row, continuous))
            if self.endpoint.observe(near_limit, row.elapsed_s):
                self.endpoint_elapsed_s = row.elapsed_s
                self.terminal_reason = "jacket_limit"
                return self.terminal_reason
        if row.elapsed_s >= self.max_elapsed_s:
            return self.check_timeout(row.elapsed_s)
        return action

    def _category_status(self, category: str) -> str:
        statuses = [entry["status"] for entry in self.checks[category].values()]
        if "FAIL" in statuses:
            return "FAIL"
        if all(status == "PASS" for status in statuses):
            return "PASS"
        return "NOT_RUN" if all(status == "NOT_RUN" for status in statuses) else "INCOMPLETE"

    def control_metrics(self) -> dict[str, Any]:
        """Time-aware observed metrics, excluding unobserved gaps from integrals."""
        metrics: dict[str, Any] = {
            "samples": len(self.control_samples), "observed_contiguous_duration_s": 0.0,
            "constrained_duration_s": 0.0, "unconstrained_duration_s": 0.0,
            "integrated_absolute_model_error_m": 0.0,
            "integrated_absolute_filtered_input_error_m": 0.0,
            "peak_model_G_m_per_s": None, "peak_filtered_input_G_m_per_s": None,
            "max_model_input_difference_m_per_s": None,
            "max_model_target_error_m_per_s": None,
            "max_filtered_input_target_error_m_per_s": None,
            "model_overshoot_percent": None, "filtered_input_overshoot_percent": None,
            "legacy_20_percent_60s_violation_observed": False,
            "legacy_rule_applied_to_paper_verdict": False,
            "tracking_episodes": [],
            "review": "Review tracking, recovery, parameter drift, cooling and supersaturation trends; no automated control-performance PASS.",
        }
        previous: AcceptanceRow | None = None
        episode: dict[str, Any] | None = None
        unconstrained_loss_start: float | None = None
        for row, contiguous in self.control_samples:
            valid_growth = _finite(row.G_model) and _finite(row.G_measure_KF)
            if not contiguous:
                if episode is not None:
                    episode.update(end_s=previous.elapsed_s, duration_s=previous.elapsed_s - episode["start_s"], recovered=False, end_reason="observation_gap")
                    metrics["tracking_episodes"].append(episode)
                    episode = None
                unconstrained_loss_start = None
            if valid_growth:
                for key, value in (
                    ("peak_model_G_m_per_s", row.G_model),
                    ("peak_filtered_input_G_m_per_s", row.G_measure_KF),
                    ("max_model_input_difference_m_per_s", abs(row.G_model - row.G_measure_KF)),
                    ("max_model_target_error_m_per_s", abs(row.G_model - 3e-8)),
                    ("max_filtered_input_target_error_m_per_s", abs(row.G_measure_KF - 3e-8)),
                ):
                    metrics[key] = value if metrics[key] is None else max(metrics[key], value)
            previous_valid = previous is not None and _finite(previous.G_model) and _finite(previous.G_measure_KF)
            if contiguous and valid_growth and previous_valid:
                dt = row.elapsed_s - previous.elapsed_s
                metrics["observed_contiguous_duration_s"] += dt
                # Count only intervals whose endpoint saturation states agree.
                if self.jacket_constrained(row) == self.jacket_constrained(previous):
                    key = "constrained_duration_s" if self.jacket_constrained(row) else "unconstrained_duration_s"
                    metrics[key] += dt
                metrics["integrated_absolute_model_error_m"] += dt * (abs(previous.G_model - 3e-8) + abs(row.G_model - 3e-8)) / 2
                metrics["integrated_absolute_filtered_input_error_m"] += dt * (abs(previous.G_measure_KF - 3e-8) + abs(row.G_measure_KF - 3e-8)) / 2
            in_band = valid_growth and self.growth_in_band(row)
            if valid_growth and not in_band:
                if episode is None:
                    episode = {"start_s": row.elapsed_s, "end_s": None, "duration_s": 0.0, "recovered": False, "end_reason": "run_end"}
                if not self.jacket_constrained(row):
                    if unconstrained_loss_start is None:
                        unconstrained_loss_start = row.elapsed_s
                    if row.elapsed_s - unconstrained_loss_start >= 60:
                        metrics["legacy_20_percent_60s_violation_observed"] = True
                else:
                    unconstrained_loss_start = None
            else:
                unconstrained_loss_start = None
                if episode is not None:
                    episode.update(end_s=row.elapsed_s, duration_s=row.elapsed_s - episode["start_s"], recovered=bool(in_band), end_reason="first_return_in_band" if in_band else "missing_growth")
                    metrics["tracking_episodes"].append(episode)
                    episode = None
            previous = row
        if episode is not None and previous is not None:
            episode.update(duration_s=previous.elapsed_s - episode["start_s"])
            metrics["tracking_episodes"].append(episode)
        for peak, result in (("peak_model_G_m_per_s", "model_overshoot_percent"), ("peak_filtered_input_G_m_per_s", "filtered_input_overshoot_percent")):
            if metrics[peak] is not None:
                metrics[result] = max(0.0, (metrics[peak] / 3e-8 - 1) * 100)
        return metrics

    def report(self) -> dict[str, Any]:
        action_status = self._category_status("procedure_actions")
        gate_status = "FAIL" if self.failure_reason else ("PASS" if self.terminal_reason == "jacket_limit" else "INCOMPLETE")
        procedure = "FAIL" if "FAIL" in (gate_status, action_status) else (
            "PASS" if gate_status == action_status == "PASS" else "INCOMPLETE"
        )
        results = {
            "integration": self._category_status("integration"),
            "procedure": procedure, "procedure_gates": gate_status,
            "filter_refresh": self._category_status("filter_refresh"),
            "control_behavior": "REVIEW_REQUIRED", "hardware": "NOT_RUN",
        }
        overall = "FAIL" if "FAIL" in results.values() else (
            "REVIEW_REQUIRED" if all(results[key] == "PASS" for key in ("integration", "procedure", "filter_refresh")) else "INCOMPLETE"
        )
        return {
            "profile": "prosys_external_paper_four_phase", "overall": overall,
            "results": results, "phase": self.phase, "phase_name": PHASE_NAMES[self.phase],
            "terminal_reason": self.terminal_reason, "failure_reason": self.failure_reason,
            "elapsed_s": self.observed_elapsed_s, "endpoint_elapsed_s": self.endpoint_elapsed_s,
            "unique_samples": self.unique_samples, "duplicate_or_old_samples": self.duplicates,
            "invalid_samples": self.invalid_samples, "continuity_breaks": self.continuity_breaks,
            "transitions": list(self.transitions), "checks": {
                category: {name: dict(check) for name, check in checks.items()}
                for category, checks in self.checks.items()
            },
            "criteria": {
                "sigma_target": 0.035, "sigma_tolerance": 0.005,
                "growth_target_m_per_s": 3e-8, "stage3_growth_tolerance_m_per_s": 6e-9,
                "stable_parameter_relative_change": 0.10, "stable_parameter_changes": 4,
                "dwell_s": 60.0, "max_observation_gap_s": self.max_gap_s,
                "max_elapsed_s": self.max_elapsed_s, "jacket_lower_K": 253.15,
                "jacket_setpoint_tolerance_K": 0.01, "jacket_actual_tolerance_K": 0.5,
                "timing": "Real elapsed time; consecutive Controller ticks with bounded scheduling jitter.",
                "paper_vs_engineering": "Stage 1/3 numeric gates and endpoint tolerances are automation conventions, not quoted paper thresholds.",
            },
            "control_metrics": self.control_metrics(),
            "not_run": ["Real GSensor camera and eight-image warmup", "Physical FBRM counts", "Real seed dissolution", "Secondary nucleation", "Real plant hardware"],
        }
