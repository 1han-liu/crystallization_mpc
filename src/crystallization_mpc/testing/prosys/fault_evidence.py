"""Pure checks of captured Prosys fault evidence; never injects a fault.

``evaluate(case, evidence)`` accepts an identity and timestamped raw captures::

    {"run_id": "...", "experiment_started_at": "...Z",
     "captures": [{"kind": "controller", "phase": "before",
                   "observed_at": "...Z", "body": <GET /api/status JSON>}, ...]}

Kinds are ``controller``, ``gsensor``, ``command`` (the original Rabbit command
envelope), ``receipt`` (the original Rabbit result envelope), ``node_values``
(ProsysProcessClient.read_values, temperatures in Celsius), ``fault_error``
({"error": <actual exception/log text>}), and ``restart`` (container inspection
with ``Id`` and ``State.StartedAt``). Phases are specified by each evaluator.
Duplicate-command checks require a deliberate different sentinel readback
between the original write and its replay, with Controller inactive throughout.
Process-loss evidence also supplies the configured ``process_sample_max_age_s``;
missing-node evidence supplies ``expected_node_ids`` and an actual fault capture
with ``node_id``. Recovery uses raw status ages/read timestamps and a newly
committed output, not a standalone availability flag.

These results describe only supplied evidence, not authenticated provenance or
an entire acceptance run. Missing records are never success. There is no HTTP,
RabbitMQ, OPC UA, subprocess, filesystem, or production control call here.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import re
from typing import Any, Mapping

from crystallization_mpc.apps.opcua_writer.commands import WriteCommand


CASES = ("stale_process", "missing_node", "broker_disconnect", "broker_availability",
         "read_only_setpoint", "duplicate_command", "growth_interruption",
         "gsensor_restart", "controller_restart")


class MissingEvidence(ValueError):
    pass


def _time(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("A timezone-qualified capture time is required.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Capture time has no timezone.")
    return parsed.timestamp()


def _finite(value: Any) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def _same(a: Any, b: Any, tolerance: float = 1e-8) -> bool:
    return _finite(a) and _finite(b) and abs(a - b) <= tolerance


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value or value[key] is None:
        raise MissingEvidence(f"Missing required evidence field: {key}.")
    return value[key]


class _Review:
    def __init__(self, case: str, evidence: Mapping[str, Any]):
        self.case, self.evidence, self.checks = case, evidence, {}
        self.run_id = evidence.get("run_id")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("Evidence needs the owned run_id.")
        self.started = _time(evidence.get("experiment_started_at"))
        self.captures = evidence["captures"]
        for item in self.captures:
            if not isinstance(item, Mapping) or not isinstance(item.get("body"), Mapping):
                raise ValueError("Capture/body must be objects.")
            _time(item.get("observed_at"))

    def get(self, kind: str, phase: str) -> Mapping[str, Any]:
        matches = [item for item in self.captures if item.get("kind") == kind and item.get("phase") == phase]
        if not matches:
            raise MissingEvidence(f"Missing {kind}/{phase} capture.")
        if len(matches) != 1:
            raise ValueError(f"Ambiguous {kind}/{phase} capture; select one exact observation.")
        return matches[0]

    def body(self, kind: str, phase: str) -> Mapping[str, Any]:
        return self.get(kind, phase)["body"]

    def check(self, name: str, condition: bool, detail: str) -> None:
        if name in self.checks:
            raise ValueError(f"Duplicate check name would overwrite evidence: {name}.")
        self.checks[name] = {"status": "PASS" if condition else "FAIL", "detail": detail}

    def ordered(self, kind: str, *phases: str) -> None:
        times = [_time(self.get(kind, phase)["observed_at"]) for phase in phases]
        self.check(f"capture_order_{kind}_{'_'.join(phases)}", all(a < b for a, b in zip(times, times[1:])),
                   "Required observations have strictly increasing real capture times.")

    def event_order(self, name: str, *events: tuple[str, str]) -> None:
        times = [_time(self.get(kind, phase)["observed_at"]) for kind, phase in events]
        self.check(name, all(a <= b for a, b in zip(times, times[1:])),
                   "Cross-source observations bracket the action, receipt, and subsequent readback.")

    def controller(self, phase: str, *, new_run: bool = False) -> Mapping[str, Any]:
        value = self.body("controller", phase)
        run_id = _required(value, "current_run_id")
        started = _time(_required(value, "started_at"))
        observed = _time(self.get("controller", phase)["observed_at"])
        identity_ok = (isinstance(run_id, str) and bool(run_id.strip()) and started <= observed
                       and ((run_id != self.run_id and started > self.started)
                            if new_run else run_id == self.run_id and started == self.started))
        self.check(f"{phase}_identity", identity_ok, "Controller run/start identity is explicit and matches the expected lifecycle.")
        return value

    def fresh_process(self, phase: str, controller: Mapping[str, Any]) -> None:
        maximum_age = _required(self.evidence, "process_sample_max_age_s")
        if not _finite(maximum_age) or maximum_age <= 0:
            raise ValueError("Configured process_sample_max_age_s must be finite and positive.")
        process = (controller.get("integrations") or {}).get("process") or {}
        age = _required(process, "sample_age_s")
        read_at = _time(_required(process, "last_read_at"))
        observed = _time(self.get("controller", phase)["observed_at"])
        self.check(f"{phase}_fresh_process", process.get("sample_valid") is True
                   and process.get("missing_fields") == [] and _finite(age) and 0 <= age <= maximum_age
                   and _time(controller["started_at"]) <= read_at <= observed and observed - read_at <= maximum_age,
                   "Both explicit sample age and raw read timestamp are fresh for this run and capture.")

    def command(self, phase: str) -> WriteCommand:
        value = WriteCommand.from_envelope(self.body("command", phase))
        self.check(f"{phase}_command_identity", value.run_id == self.run_id
                   and value.run_started_at.timestamp() == self.started,
                   "Command envelope passes production parsing and owned run/start checks.")
        observed = _time(self.get("command", phase)["observed_at"])
        self.check(f"{phase}_command_time", self.started <= observed < value.expires_at.timestamp(),
                   "The test command was captured in its owned run before expiration.")
        return value

    def receipt(self, phase: str, command: WriteCommand) -> Mapping[str, Any]:
        envelope = self.body("receipt", phase)
        value = envelope.get("payload")
        if not isinstance(value, Mapping):
            raise ValueError("Receipt payload must be an object.")
        valid = (envelope.get("ver") == 1 and envelope.get("name") == "process.setpoint.result"
                 and envelope.get("src") == "opcua_writer" and envelope.get("dst") == "process"
                 and envelope.get("msg_type") == "event"
                 and envelope.get("correlation_id") == command.command_id
                 and value.get("command_id") == command.command_id
                 and value.get("run_id") == self.run_id
                 and _time(value.get("run_started_at")) == self.started
                 and type(value.get("tick_seq")) is int and value["tick_seq"] == command.tick_seq)
        self.check(f"{phase}_receipt_identity", valid, "Actual Rabbit result envelope is correlated to the original command.")
        return value

    def process_loss(self) -> None:
        before, failed = self.controller("before"), self.controller("fault")
        after = self.controller("after")
        self.ordered("controller", "before", "fault", "after")
        process = (before.get("integrations") or {}).get("process") or {}
        self.check("healthy_baseline", before.get("active") is True and process.get("sample_valid") is True,
                   "The fault follows a running Controller with valid process input.")
        self.fresh_process("before", before)
        self.check("terminal_failure", all(value.get("status") == "error" and value.get("active") is False
                   for value in (failed, after)), "Process failure stops Controller; later observation does not silently resume it.")
        error = str(failed.get("last_error", "")) + str(((failed.get("integrations") or {}).get("process") or {}).get("service_last_error", ""))
        self.check("fault_reason", "process" in error.lower() and any(word in error.lower() for word in
                   ("expired", "measurement", "read", "connection", "write", "timeout", "unknown")),
                   "Actual Controller error identifies process input or command failure.")
        old, broken, later = (value.get("last_control_output") or {} for value in (before, failed, after))
        self.check("no_false_new_success", ((broken.get("tick_seq") == old.get("tick_seq") and bool(old))
                   or (broken.get("result") or {}).get("valid") is False) and later == broken,
                   "No new successful committed output appears after the terminal fault.")
        recovered = self.controller("recovered", new_run=True)
        self.ordered("controller", "before", "fault", "after", "recovered")
        recovery_process = (recovered.get("integrations") or {}).get("process") or {}
        self.check("fresh_new_run_recovery", recovered.get("active") is True
                   and recovered.get("status") == "running" and recovery_process.get("sample_valid") is True
                   and recovery_process.get("missing_fields") == [],
                   "Recovery is a distinct later run with complete fresh process input.")
        self.fresh_process("recovered", recovered)
        recovery_output = _required(recovered, "last_control_output")
        self.check("recovered_committed_output", recovery_output.get("run_id") == recovered.get("current_run_id")
                   and type(recovery_output.get("tick_seq")) is int and recovery_output["tick_seq"] > 0
                   and (recovery_output.get("result") or {}).get("valid") is True,
                   "The new run has its own valid committed control cycle, not the previous run's output.")
        if self.case == "stale_process":
            # read_state failure disconnects the production adapter and clears
            # its cached age. An actual earlier status capture can retain the
            # expired-input proof; never reconstruct it from a later error.
            has_expired_capture = any(item.get("kind") == "controller" and item.get("phase") == "expired_input"
                                      for item in self.captures)
            stale_phase = "expired_input" if has_expired_capture else "fault"
            expired = self.controller("expired_input") if has_expired_capture else failed
            stale = (expired.get("integrations") or {}).get("process") or {}
            age = _required(stale, "sample_age_s")
            read_at = _time(_required(stale, "last_read_at"))
            fault_time = _time(self.get("controller", stale_phase)["observed_at"])
            if has_expired_capture:
                self.ordered("controller", "before", "expired_input", "fault", "after")
                self.check("expired_input_same_live_source", expired.get("active") is True
                           and expired.get("status") == "running" and stale.get("connected") is True
                           and stale.get("missing_fields") == [] and stale.get("node_ids") == process.get("node_ids")
                           and isinstance(stale.get("node_ids"), Mapping) and len(stale["node_ids"]) == 5
                           and self.started <= read_at <= fault_time,
                           "The raw pre-disconnect capture proves the same running five-node process expired, not missing fields or another run.")
            self.check("actual_stale_process", stale.get("sample_valid") is False and _finite(age)
                       and age > self.evidence["process_sample_max_age_s"]
                       and fault_time - read_at > self.evidence["process_sample_max_age_s"]
                       and "expired" in error.lower(),
                       "This case proves actual expired input, not a different generic write/connection failure.")
            self.stale_injection_and_recovery(failed)
        if self.case == "missing_node":
            fault = self.body("fault_error", "fault")
            expected = _required(self.evidence, "expected_node_ids")
            node_id = _required(fault, "node_id")
            self.check("actual_missing_node", isinstance(expected, Mapping) and len(expected) == 5
                       and expected == process.get("node_ids") and node_id in expected.values()
                       and "BadNodeIdUnknown" in str(fault.get("error", "")),
                       "The actual missing node belongs to the expected five-node process contract.")
            self.event_order("missing_node_fault_window", ("controller", "before"), ("fault_error", "fault"),
                             ("controller", "after"))
        if self.case == "broker_disconnect":
            command = self.command("fault")
            receipt = self.receipt("fault", command)
            self.check("no_success_for_uncertain_command", receipt.get("status") in {"failed", "unknown"}
                       and receipt.get("write_result") is None,
                       "An interrupted command remains failed/unknown, never a fabricated successful write.")
            self.event_order("broker_command_fault_window", ("controller", "before"), ("command", "fault"),
                             ("receipt", "fault"), ("controller", "fault"))
            self.check("broker_fault_new_command", type(old.get("tick_seq")) is int and command.tick_seq > old["tick_seq"],
                       "The interrupted command follows, rather than reuses, the healthy baseline cycle.")

    def stale_injection_and_recovery(self, failed: Mapping[str, Any]) -> None:
        """Require the actual pause and recovered input, not merely an error string."""
        containers = [self.body("telegraf", phase) for phase in ("before", "paused", "resumed")]
        identifier = _required(containers[0], "Id")
        self.check("stale_same_isolated_telegraf", isinstance(identifier, str)
                   and bool(re.fullmatch(r"[0-9a-f]{64}", identifier))
                   and all(item.get("Id") == identifier
                           and str(item.get("Name", "")).lstrip("/") == "mpcrystal-prosys_telegraf_1" for item in containers),
                   "Pause and recovery identify the same isolated Telegraf container.")
        for index, item in enumerate(containers):
            labels = _required(item, "Labels")
            for suffix, expected in (("project", "mpcrystal-prosys"), ("service", "telegraf")):
                values = [labels[key] for key in (f"io.podman.compose.{suffix}", f"com.docker.compose.{suffix}") if labels.get(key)]
                self.check(f"stale_container_{index}_{suffix}", bool(values) and all(value == expected for value in values),
                           "All supplied compose labels match the exact test project/service.")
        before, paused, resumed = [_required(item, "State") for item in containers]
        self.check("actual_same_process_pause_and_resume", all(state.get("Running") is True
                   and state.get("Paused") is False and state.get("Status") == "running" for state in (before, resumed))
                   and paused.get("Paused") is True and paused.get("Status") == "paused"
                   and type(paused.get("Running")) is bool
                   and _time(before.get("StartedAt")) == _time(paused.get("StartedAt")) == _time(resumed.get("StartedAt")),
                   "Actual Podman/Docker paused state and unchanged process StartedAt bracket one pause/unpause.")
        self.event_order("stale_injection_window", ("telegraf", "before"), ("controller", "before"),
                         ("telegraf", "paused"), ("controller", "fault"), ("telegraf", "resumed"),
                         ("telegraf", "recovered_input"), ("controller", "after"))
        if any(item.get("kind") == "controller" and item.get("phase") == "expired_input" for item in self.captures):
            self.event_order("stale_expiry_after_pause", ("telegraf", "paused"), ("controller", "expired_input"),
                             ("controller", "fault"))
        from .raw_store import timestamp_ns
        proof = self.get("telegraf", "recovered_input")
        nodes = _required(self.evidence, "expected_node_ids")
        baseline_nodes = (self.body("controller", "before").get("integrations", {}).get("process") or {}).get("node_ids")
        self.check("stale_recovery_node_identity", isinstance(nodes, Mapping)
                   and set(nodes) == {"T", "T_j", "c", "count_middle", "T_j_set"}
                   and proof["body"].get("node_ids") == nodes == baseline_nodes,
                   "Independent recovered collections use the same verified five-node contract.")
        rounds = _required(proof["body"], "rounds")
        if not isinstance(rounds, list) or len(rounds) != 3:
            raise MissingEvidence("Exactly three actual independent recovered Telegraf rounds are required.")
        stamps = []
        for index, row in enumerate(rounds):
            stamp = timestamp_ns(_required(row, "read_at"))
            stamps.append(stamp)
            age = _time(proof["observed_at"]) - _time(row["read_at"])
            self.check(f"stale_fresh_round_{index}", stamp > timestamp_ns(self.get("telegraf", "resumed")["observed_at"])
                       and type(row.get("collection_timestamp_ns")) is int and row["collection_timestamp_ns"] == stamp
                       and 0 <= age <= self.evidence["process_sample_max_age_s"]
                       and all(_finite(row.get(key)) for key in nodes)
                       and all((row.get("status_codes") or {}).get(key) in {"OK (0x0)", "The operation succeeded. StatusGood (0x0)"} for key in nodes)
                       and all(type(row.get(key)) is int and row[key] >= 0 for key in ("invalid_message_count", "reconnect_count")),
                       "Actual round timestamps, finite five-node values and Good quality establish fresh recovery.")
        self.check("stale_three_consecutive_rounds", all(0 < b - a <= 2_500_000_000 for a, b in zip(stamps, stamps[1:]))
                   and all(row[key] == rounds[0][key] for row in rounds for key in ("invalid_message_count", "reconnect_count")),
                   "Three distinct recovered rounds have no invalid-message/reconnect increments.")
        dt = _required(self.body("controller", "before").get("scheduler", {}), "controller_dt_s")
        proof_at, after_at = _time(proof["observed_at"]), _time(self.get("controller", "after")["observed_at"])
        self.check("stale_noresume_observation_duration", _finite(dt) and dt > 0 and after_at - proof_at >= 2 * dt,
                   "The old run is observed for at least two control periods after independent fresh input returns.")
        records = _required(self.evidence, "interim_observations")
        if not isinstance(records, list):
            raise ValueError("Intermediate Controller observations must be a list.")
        recovery_times = []
        for index, item in enumerate(records):
            observed_at = _time(_required(item, "observed_at"))
            if observed_at < _time(self.get("controller", "fault")["observed_at"]):
                continue
            state = _required(item, "body")
            self.check(f"stale_no_interim_resume_{index}", state.get("current_run_id") == self.run_id
                       and _time(_required(state, "started_at")) == self.started
                       and state.get("active") is False and state.get("status") == "error"
                       and state.get("last_control_output") == failed.get("last_control_output"),
                       "No supplied observation after failure can hide an active old run or changed output.")
            if proof_at <= observed_at <= after_at:
                recovery_times.append(observed_at)
        self.check("stale_recovery_observations_cover_window", len(recovery_times) >= 2
                   and max(recovery_times) - min(recovery_times) >= 2 * dt
                   and all(a < b for a, b in zip(recovery_times, recovery_times[1:])),
                   "Actual ordered Controller observations cover the required post-recovery nonresume window.")

    def broker_availability(self) -> None:
        """Broker outage/stop/recovery only; makes no Writer-receipt claim."""
        healthy, failed, after = (self.controller(phase) for phase in ("before", "fault", "after"))
        self.ordered("controller", "before", "fault", "after")
        self.check("healthy_broker_baseline", healthy.get("active") is True
                   and (healthy.get("integrations", {}).get("process") or {}).get("connected") is True,
                   "An active Controller with a connected process bus precedes the outage.")
        self.fresh_process("before", healthy)
        self.check("broker_terminal_failure", all(item.get("status") == "error" and item.get("active") is False for item in (failed, after)),
                   "Process loss produces a terminal Controller error without automatic continuation.")
        error = str(failed.get("last_error", "")) + str(((failed.get("integrations") or {}).get("process") or {}).get("service_last_error", ""))
        self.check("broker_process_fault_reason", "process" in error.lower() and any(word in error.lower() for word in ("read", "connection", "measurement", "timeout", "write", "unknown", "expired")),
                   "The actual error identifies process I/O failure.")
        old, broken, later = (item.get("last_control_output") or {} for item in (healthy, failed, after))
        self.check("broker_terminal_output_unchanged", bool(old) and later == broken,
                   "The last output cannot advance after the terminal failure, including during recovery.")
        recovered = self.controller("recovered", new_run=True)
        self.ordered("controller", "before", "fault", "after", "recovered")
        self.fresh_process("recovered", recovered)
        recovered_output = _required(recovered, "last_control_output")
        self.check("broker_new_run_recovered", recovered.get("active") is True and recovered.get("status") == "running"
                   and recovered_output.get("run_id") == recovered.get("current_run_id")
                   and type(recovered_output.get("tick_seq")) is int and recovered_output["tick_seq"] > 0
                   and (recovered_output.get("result") or {}).get("valid") is True,
                   "A distinct later run receives fresh input and commits its own valid cycle.")
        before, stopped, restored = (self.body("broker", phase) for phase in ("before", "stopped", "restored"))
        identifier = _required(before, "Id")
        self.check("same_isolated_broker", isinstance(identifier, str) and len(identifier) == 64
                   and all(item.get("Id") == identifier and str(item.get("Name", "")).lstrip("/") == "mpcrystal-prosys_rabbitmq_1"
                           for item in (before, stopped, restored)),
                   "The stopped and restarted broker is the exact original isolated container, not a replacement.")
        for index, item in enumerate((before, stopped, restored)):
            labels = _required(item, "Labels")
            for suffix, expected in (("project", "mpcrystal-prosys"), ("service", "rabbitmq")):
                values = [labels[key] for key in (f"io.podman.compose.{suffix}", f"com.docker.compose.{suffix}") if labels.get(key)]
                self.check(f"broker_{index}_{suffix}_identity", bool(values) and all(value == expected for value in values),
                           "Compose labels identify only the isolated RabbitMQ service.")
        states = [_required(item, "State") for item in (before, stopped, restored)]
        self.check("actual_broker_stop_and_start", all(state.get("Running") is True and state.get("Paused") is False
                   and state.get("Status") == "running" for state in (states[0], states[2]))
                   and states[1].get("Running") is False and states[1].get("Paused") is False and states[1].get("Status") == "exited"
                   and _time(states[0]["StartedAt"]) == _time(states[1]["StartedAt"])
                   and _time(states[2]["StartedAt"]) > _time(states[1]["FinishedAt"]),
                   "Actual inspection proves stopped, not merely paused, followed by a newer process start.")
        self.check("broker_stop_inside_capture", _time(self.get("controller", "before")["observed_at"])
                   <= _time(states[1]["FinishedAt"]) <= _time(self.get("broker", "stopped")["observed_at"]),
                   "The real process exit lies after the healthy Controller and before the stopped inspection.")
        disconnected = self.controller("disconnected")
        process = (disconnected.get("integrations") or {}).get("process") or {}
        self.check("actual_process_bus_disconnected", process.get("connected") is False and process.get("sample_valid") is False,
                   "Raw Controller status observes loss of its process-bus connection.")
        self.event_order("broker_outage_window", ("controller", "before"), ("broker", "stopped"),
                         ("controller", "disconnected"), ("controller", "fault"), ("broker", "restored"), ("controller", "after"))
        dt = _required(self.body("controller", "before").get("scheduler", {}), "controller_dt_s")
        waited = _time(self.get("controller", "after")["observed_at"]) - _time(self.get("broker", "restored")["observed_at"])
        self.check("broker_recovery_nonresume_window", _finite(dt) and dt > 0 and waited >= 2 * dt,
                   "After broker recovery the old run stays stopped for at least two control periods.")
        for index, record in enumerate(self.evidence.get("recovery_observations", [])):
            observed = _required(record, "body")
            self.check(f"broker_no_interim_resume_{index}", observed.get("current_run_id") == self.run_id
                       and _time(_required(observed, "started_at")) == self.started
                       and observed.get("active") is False and observed.get("status") == "error"
                       and observed.get("last_control_output") == failed.get("last_control_output"),
                       "No supplied intermediate recovery observation may hide automatic continuation.")
        # Validate actual independent five-node collections, not a boolean PASS
        # copied from a readiness helper. This is separate from Writer outcome.
        from .raw_store import timestamp_ns
        proof = self.get("telegraf", "recovered_input")
        body = proof["body"]
        nodes = self.evidence.get("expected_node_ids")
        self.check("broker_recovery_node_identity", isinstance(nodes, Mapping)
                   and set(nodes) == {"T", "T_j", "c", "count_middle", "T_j_set"}
                   and body.get("node_ids") == nodes == self.body("controller", "before")["integrations"]["process"].get("node_ids"),
                   "Recovered raw collections use the same verified five-node mapping.")
        rounds = _required(body, "rounds")
        if not isinstance(rounds, list) or len(rounds) != 3:
            raise MissingEvidence("Exactly three independent broker-recovery collections are required.")
        stamps = []
        for index, row in enumerate(rounds):
            stamp = timestamp_ns(_required(row, "read_at"))
            stamps.append(stamp)
            age = _time(proof["observed_at"]) - _time(row["read_at"])
            self.check(f"broker_fresh_round_{index}", stamp > timestamp_ns(self.get("broker", "restored")["observed_at"])
                       and type(row.get("collection_timestamp_ns")) is int and row["collection_timestamp_ns"] == stamp
                       and 0 <= age <= self.evidence["process_sample_max_age_s"]
                       and all(_finite(row.get(key)) for key in nodes)
                       and all((row.get("status_codes") or {}).get(key) in {"OK (0x0)", "The operation succeeded. StatusGood (0x0)"} for key in nodes)
                       and all(type(row.get(key)) is int and row[key] >= 0 for key in ("invalid_message_count", "reconnect_count")),
                       "The independent subscriber records real fresh, finite, Good-quality five-node values.")
        self.check("broker_three_consecutive_rounds", all(0 < b - a <= 2_500_000_000 for a, b in zip(stamps, stamps[1:]))
                   and all(row[key] == rounds[0][key] for row in rounds for key in ("invalid_message_count", "reconnect_count")),
                   "Three distinct collections recover without invalid-message or reconnect increments.")
        if broken != old and (broken.get("result") or {}).get("valid") is True:
            # A graceful stop can overlap a legitimately completed cycle. Do
            # not relabel that as a success emitted while the broker was off.
            self.check("broker_pre_exit_output_identity", broken.get("run_id") == self.run_id
                       and type(broken.get("tick_seq")) is int and type(old.get("tick_seq")) is int
                       and broken["tick_seq"] > old["tick_seq"],
                       "A changed valid output must be a genuine later cycle of this run.")
            write = _required(broken, "process_write")
            if (write.get("verified") is not True
                    or _time(_required(broken, "computed_at")) > _time(states[1]["FinishedAt"])
                    or _time(_required(write, "written_at")) > _time(states[1]["FinishedAt"])):
                raise MissingEvidence("A successful cycle straddles the broker exit boundary; its confirmation timing is unproven.")
            self.check("broker_success_completed_before_exit", True,
                       "Both the valid cycle and verified device write completed before the actual broker FinishedAt.")
        elif broken != old:
            self.check("broker_failed_write_not_success", (broken.get("result") or {}).get("valid") is False,
                       "A new fault-cycle output is explicitly invalid, never a fabricated successful write.")

    def readonly(self) -> None:
        command = self.command("fault")
        receipt = self.receipt("fault", command)
        failed = self.controller("fault")
        output = failed.get("last_control_output") or {}
        self.check("definite_write_rejection", receipt.get("status") == "failed"
                   and receipt.get("write_result") is None and any(code in str(receipt.get("error", ""))
                   for code in ("BadNotWritable", "BadUserAccessDenied")),
                   "Production Writer reports the actual read-only/access-denied OPC UA rejection.")
        self.check("controller_rejected_cycle", failed.get("active") is False and failed.get("status") == "error"
                   and output.get("run_id") == self.run_id
                   and output.get("tick_seq") == command.tick_seq
                   and (output.get("result") or {}).get("valid") is False
                   and bool(output.get("process_write_error")) and output.get("diagnostics") is None,
                   "The matching Controller cycle is invalid and cannot publish successful refresh proof.")
        self.ordered("node_values", "before", "after")
        self.event_order("readonly_command_receipt_readback", ("node_values", "before"), ("command", "fault"),
                         ("receipt", "fault"), ("controller", "fault"), ("node_values", "after"))
        before, after = (self.body("node_values", phase).get("T_j_set") for phase in ("before", "after"))
        self.check("readback_unchanged", _same(before, after) and not _same(before + 273.15, command.setpoint_K, .05)
                   if _finite(before) else False,
                   "Independent actual readback stays at the previous value, distinct from the rejected command.")

    def duplicate(self) -> None:
        original, replay = self.command("original"), self.command("replay")
        first, second = self.receipt("original", original), self.receipt("replay", replay)
        self.ordered("command", "original", "replay")
        self.event_order("original_write_readback", ("command", "original"), ("receipt", "original"),
                         ("node_values", "original"), ("node_values", "sentinel"))
        self.event_order("replay_completed_before_readback", ("command", "replay"), ("receipt", "replay"),
                         ("node_values", "replay"))
        self.check("identical_replay", original.fingerprint() == replay.fingerprint(),
                   "The exact same command ID and payload were replayed, not a new command.")
        write = first.get("write_result") or {}
        self.check("cached_verified_receipt", first == second and first.get("status") == "success"
                   and write.get("verified") is True and _same(write.get("setpoint_K"), original.setpoint_K, .05)
                   and _same(write.get("readback_K"), original.setpoint_K, .05),
                   "The second delivery returns the original verified terminal receipt.")
        written_at = _time(_required(write, "written_at"))
        self.check("original_write_completion_time", _time(self.get("command", "original")["observed_at"]) <= written_at
                   <= _time(self.get("receipt", "original")["observed_at"]),
                   "Original write completion falls after its command and before its captured terminal receipt.")
        if self.evidence.get("mode") == "writer_fixture":
            self.writer_fixture_guards()
        else:
            if self.evidence.get("mode") not in (None, "legacy"):
                raise ValueError("Unsupported duplicate-command evidence mode.")
            inactive = [self.controller(phase) for phase in ("original", "sentinel", "replay")]
            self.check("controller_inactive", all(value.get("active") is False for value in inactive),
                       "No running Controller competes with this dedicated duplicate-command fixture.")
        self.ordered("node_values", "original", "sentinel", "replay")
        initial, sentinel, last = (self.body("node_values", phase).get("T_j_set")
                                   for phase in ("original", "sentinel", "replay"))
        self.check("duplicate_did_not_rewrite", _finite(initial) and _same(initial + 273.15, original.setpoint_K, .05)
                   and _finite(sentinel) and not _same(initial, sentinel, .1) and _same(last, sentinel),
                   "Actual node readback remains at a different sentinel value after replay; cached success alone is insufficient.")
        self.check("sentinel_before_replay", _time(self.get("node_values", "sentinel")["observed_at"])
                   < _time(self.get("command", "replay")["observed_at"])
                   < _time(self.get("node_values", "replay")["observed_at"]),
                   "Sentinel readback precedes replay, and final readback follows it.")

    def writer_fixture_guards(self) -> None:
        """A standalone Writer test never impersonates Controller's old run."""
        import json
        baseline = {kind: self.body(kind, "before") for kind in ("controller", "central", "plant")}
        baseline_current = baseline["central"].get("current_experiment")
        original_ids = {baseline["controller"].get("current_run_id"), baseline["plant"].get("run_id")}
        if isinstance(baseline_current, Mapping):
            original_ids.add(baseline_current.get("run_id"))
        self.check("writer_fixture_has_new_identity", self.run_id not in original_ids,
                   "Writer fixture identity is distinct from every preserved Controller/Central/plant run.")
        for phase in ("before", "original", "sentinel", "replay"):
            controller, central, plant = (self.body(kind, phase) for kind in ("controller", "central", "plant"))
            current = central.get("current_experiment")
            self.check(f"writer_fixture_inactive_guards_{phase}",
                controller.get("active") is False and controller.get("status") in {"idle", "configured", "stopped", "error"}
                and (controller.get("integrations") or {}).get("process", {}).get("write_in_flight") is False
                and (controller.get("current_run_id"), controller.get("started_at"), controller.get("status"))
                    == (baseline["controller"].get("current_run_id"), baseline["controller"].get("started_at"), baseline["controller"].get("status"))
                and "current_experiment" in central and current == baseline_current
                and (current is None or (isinstance(current, Mapping) and current.get("status") in {"completed", "error"}))
                and plant.get("source") == "Synthetic G / Model-generated input" and plant.get("running") is False
                and plant.get("status") in {"unprepared", "stopped", "error"}
                and all(plant.get(key) == baseline["plant"].get(key) for key in ("run_id", "experiment_started_at", "status")),
                "All three real services remain inactive under their original identities; no identity is rewritten to the Writer fixture.")
        first, second = self.get("command", "original"), self.get("command", "replay")
        raw_first, raw_second = _required(first, "raw_utf8"), _required(second, "raw_utf8")
        self.check("writer_fixture_identical_wire_bytes", isinstance(raw_first, str) and raw_first == raw_second
                   and json.loads(raw_first) == first["body"] == second["body"],
                   "The only planned replay sends exactly the original UTF-8 bytes, not a rebuilt envelope.")
        for phase in ("original", "replay"):
            receipt = self.get("receipt", phase)
            self.check(f"writer_fixture_receipt_after_publish_{phase}",
                _time(self.get("command", phase)["observed_at"]) <= _time(receipt["body"].get("ts"))
                <= _time(receipt["observed_at"]),
                "Writer generated the captured result envelope between that publish attempt and its actual observation.")

    def growth(self) -> None:
        before, during, recovered = (self.controller(phase) for phase in ("before", "fault", "recovered"))
        self.ordered("controller", "before", "fault", "recovered")
        self.check("live_G_baseline", before.get("control_target") == "G"
                   and (before.get("growth_input") or {}).get("source") == "live_gsensor"
                   and (before.get("growth_input") or {}).get("available") is True
                   and (before.get("adaptation") or {}).get("active") is True,
                   "Interruption starts from available live growth-rate control.")
        growth = during.get("growth_input") or {}
        output = during.get("last_control_output") or {}
        result, process = output.get("result") or {}, output.get("process_state") or {}
        diagnostic = output.get("diagnostics") or {}
        fields = diagnostic.get("fields") or {}
        self.check("G_gate_closed", during.get("active") is True and during.get("status") == "running"
                   and during.get("control_target") == "G" and growth.get("source") == "live_gsensor"
                   and growth.get("available") is False and bool(growth.get("control_hold_reason"))
                   and (during.get("adaptation") or {}).get("active") is False,
                   "Invalid/stale G closes control and fitting gates.")
        self.check("committed_hold", output.get("run_id") == self.run_id
                   and output.get("tick_seq", -1) > (before.get("last_control_output") or {}).get("tick_seq", 0)
                   and diagnostic.get("tick_seq") == output.get("tick_seq") and diagnostic.get("run_id") == self.run_id
                   and result.get("valid") is True
                   and fields.get("diag_G_lifecycle") == "paused" and bool(fields.get("diag_control_hold_reason"))
                   and result.get("G_measure_KF") is None and _same(result.get("T_j_set"), process.get("T_j_set")),
                   "A new committed cycle pauses G filtering and reasserts measured equipment setpoint, not stale optimized output.")
        self.check("fresh_G_recovery", (recovered.get("growth_input") or {}).get("available") is True
                   and recovered.get("last_frame_seq", -1) > before.get("last_frame_seq", 0)
                   and (recovered.get("growth_input") or {}).get("control_event_id") == growth.get("control_event_id"),
                   "A later fresh frame with the same activation identity restores availability.")
        recovered_growth = recovered.get("growth_input") or {}
        age = _required(recovered_growth, "sample_age_s")
        growth_dt = _required(recovered.get("scheduler") or {}, "growth_dt_s")
        activation = _required(growth, "control_event_id")
        recovered_output = _required(recovered, "last_control_output")
        recovered_result = recovered_output.get("result") or {}
        recovered_diagnostic = recovered_output.get("diagnostics") or {}
        recovered_fields = recovered_diagnostic.get("fields") or {}
        fresh_sample = _required(recovered, "last_valid_sample")
        sampled_at = _time(_required(fresh_sample, "captured_at"))
        recovery_at = _time(self.get("controller", "recovered")["observed_at"])
        self.check("active_fresh_G_recovery", recovered.get("active") is True and recovered.get("status") == "running"
                   and recovered.get("control_target") == "G" and recovered_growth.get("source") == "live_gsensor"
                   and (recovered.get("adaptation") or {}).get("active") is True
                   and isinstance(activation, str) and bool(activation)
                   and activation == (before.get("growth_input") or {}).get("control_event_id")
                   and _finite(growth_dt) and growth_dt > 0 and _finite(age) and 0 <= age <= 2 * growth_dt,
                   "Recovery is active live-G control with the original activation and an explicitly fresh sample.")
        self.check("recovered_G_sample_identity_and_age", fresh_sample.get("run_id") == self.run_id
                   and _time(_required(fresh_sample, "experiment_started_at")) == self.started
                   and fresh_sample.get("valid") is True
                   and type(fresh_sample.get("frame_seq")) is int
                   and fresh_sample["frame_seq"] == recovered.get("last_frame_seq")
                   and _finite(growth_dt) and growth_dt > 0 and 0 <= recovery_at - sampled_at <= 2 * growth_dt
                   and sampled_at > _time(self.get("controller", "fault")["observed_at"]),
                   "Raw last-valid-sample identity and capture time corroborate the fresh post-fault frame.")
        self.check("G_recovery_new_committed_cycle", recovered_output.get("run_id") == self.run_id
                   and type(recovered_output.get("tick_seq")) is int
                   and recovered_output["tick_seq"] > output.get("tick_seq", 0)
                   and recovered_diagnostic.get("run_id") == self.run_id
                   and recovered_diagnostic.get("tick_seq") == recovered_output.get("tick_seq")
                   and recovered_result.get("valid") is True and _finite(recovered_result.get("G_measure_KF"))
                   and recovered_fields.get("diag_G_lifecycle") in {"updated", "reanchored_updated"},
                   "A later committed cycle resumes G filtering; availability or a newer frame alone is insufficient.")

    def restart(self) -> None:
        before_restart, after_restart = (self.body("restart", phase) for phase in ("before", "after"))
        self.ordered("restart", "before", "after")
        self.check("actual_restart_boundary", isinstance(before_restart.get("Id"), str) and bool(before_restart.get("Id"))
                   and after_restart.get("Id") == before_restart["Id"]
                   and _time((after_restart.get("State") or {}).get("StartedAt"))
                   > _time((before_restart.get("State") or {}).get("StartedAt")),
                   "Inspection proves a newer process start of the same container, not another container or two status polls.")
        restarted_at = _time((after_restart.get("State") or {}).get("StartedAt"))
        self.check("restart_capture_brackets_start", _time(self.get("restart", "before")["observed_at"]) < restarted_at
                   <= _time(self.get("restart", "after")["observed_at"]),
                   "The inspected newer process start lies between the two inspection captures.")
        status_kind = "gsensor" if self.case == "gsensor_restart" else "controller"
        self.check("service_capture_brackets_start", _time(self.get(status_kind, "before")["observed_at"]) < restarted_at
                   <= _time(self.get(status_kind, "after")["observed_at"]),
                   "The service status before/after observations bracket that same restart.")
        if self.case == "gsensor_restart":
            before, after = (self.body("gsensor", phase) for phase in ("before", "after"))
            self.ordered("gsensor", "before", "after")
            self.check("same_synthetic_identity", all(value.get("synthetic") is True
                       and value.get("current_run_id") == self.run_id
                       and _time(value.get("experiment_started_at")) == self.started for value in (before, after)),
                       "Both observations identify the explicit synthetic service and same experiment.")
            self.check("restart_fails_closed", before.get("active") is True and after.get("active") is False
                       and after.get("measurement_ready") is False
                       and after.get("experiment_lifecycle_status") == "error"
                       and (after.get("recovery") or {}).get("requires_new_run") is True,
                       "Synthetic G cannot silently resume an active experiment after restart.")
        else:
            before, after = self.controller("before"), self.controller("after")
            self.ordered("controller", "before", "after")
            dt = (before.get("scheduler") or {}).get("controller_dt_s")
            waited = _time(self.get("controller", "after")["observed_at"]) - _time((after_restart.get("State") or {}).get("StartedAt"))
            self.check("restart_observation_window", _finite(dt) and dt > 0 and waited >= 2 * dt,
                       "Post-restart observation covers at least two configured control periods.")
            self.check("active_before_restart", before.get("active") is True,
                       "This case actually restarts an active Controller.")
            # No approval waiver here: test is specifically restart WITHOUT a
            # follow-up operator action. A valid restored checkpoint is not
            # permission to resume an experiment.
            self.check("no_unconfirmed_resume", after.get("active") is False and after.get("status") != "running",
                       "Approved requirement forbids automatic continuation; restored-and-running is a failure, not a waiver.")
            old, new = _required(before, "last_control_output"), _required(after, "last_control_output")
            old_tick, new_tick = _required(old, "tick_seq"), _required(new, "tick_seq")
            self.check("no_new_output_without_confirmation", type(old_tick) is int and type(new_tick) is int
                       and old_tick == new_tick and old.get("run_id") == new.get("run_id") == self.run_id,
                       "No later committed cycle appears before explicit operator confirmation.")
            for index, observation in enumerate(self.evidence.get("interim_observations", [])):
                observed = _required(observation, "body")
                output = observed.get("last_control_output") or {}
                resumed = observed.get("active") is True or observed.get("status") == "running"
                newer_output = (not resumed and type(output.get("tick_seq")) is int
                    and output["tick_seq"] > old_tick
                    and _time(_required(output, "computed_at")) >= restarted_at)
                self.check(f"interim_no_unconfirmed_resume_{index + 1}",
                    _time(observation["observed_at"]) >= restarted_at and not resumed and not newer_output,
                    "Every supplied post-restart observation must remain nonrunning without newly committed output, even if the final capture is stopped.")
            # An attempted automatic restore can fail before its first tick
            # when Telegraf is unavailable. That inactivity is not proof of an
            # intentional confirmation hold. Use existing runtime fields only;
            # missing/failed recovery evidence stays INCOMPLETE, while an
            # already observed resumed cycle above retains FAIL precedence.
            recovery = _required(after, "recovery")
            if after.get("status") == "recovery_required":
                scheduler = _required(after, "scheduler")
                self.check("explicit_restart_confirmation_hold",
                    recovery.get("status") == "confirmation_required"
                    and "error" in recovery and recovery["error"] is None
                    and _required(recovery, "requires_confirmation") is True
                    and _required(recovery, "requires_new_run") is True
                    and "deadline_set" in scheduler and scheduler["deadline_set"] is False,
                    "An intentional recovery hold requires ending the old run and creating a new run; no scheduler deadline is armed.")
                old_count = _required(_required(before, "scheduler"), "tick_count")
                new_count = _required(scheduler, "tick_count")
                self.check("held_scheduler_did_not_advance", type(old_count) is int and type(new_count) is int
                           and old_count == new_count,
                           "The scheduler counter as well as the committed output is unchanged.")
                self.independent_telegraf_after_restart(restarted_at)
                return
            if (after.get("status") not in {"idle", "configured", "stopped"}
                    or recovery.get("status") != "restored"
                    or "error" not in recovery or recovery["error"] is not None):
                raise MissingEvidence("Healthy nonrunning restart hold is unproven: checkpoint recovery is absent, failed, or not ready.")
            process = (after.get("integrations") or {}).get("process") or {}
            maximum_age = _required(self.evidence, "process_sample_max_age_s")
            age = _required(process, "sample_age_s")
            read_at = _time(_required(process, "last_read_at"))
            observed_at = _time(self.get("controller", "after")["observed_at"])
            if (not _finite(maximum_age) or maximum_age <= 0 or process.get("connected") is not True
                    or process.get("sample_valid") is not True or process.get("missing_fields") != []
                    or not _finite(age) or not 0 <= age <= maximum_age
                    or not restarted_at <= read_at <= observed_at or observed_at - read_at > maximum_age):
                raise MissingEvidence("Healthy nonrunning restart hold is unproven: process input is stale, incomplete, disconnected, or predates restart.")
            self.check("healthy_nonrunning_restart_hold", True,
                       "Existing status proves restored non-error state and fresh connected process input; inactivity is not explained by a startup failure.")

    def independent_telegraf_after_restart(self, restarted_at: float) -> None:
        """A deliberately disconnected Controller needs independent live input proof."""
        from .raw_store import timestamp_ns
        record = self.get("telegraf", "after")
        body = record["body"]
        expected = _required(self.evidence, "expected_node_ids")
        baseline = self.body("controller", "before").get("integrations", {}).get("process", {}).get("node_ids")
        self.check("independent_reader_identity", body.get("source") == "Independent TelegrafRoundReader"
                   and body.get("run_id") == self.run_id
                   and _time(_required(body, "experiment_started_at")) == self.started
                   and isinstance(expected, Mapping) and set(expected) == {"T", "T_j", "c", "count_middle", "T_j_set"}
                   and len(set(expected.values())) == 5 and body.get("node_ids") == expected == baseline,
                   "The dedicated subscriber uses the same five-node mapping as the pre-restart Controller and verified Prosys identity.")
        rounds = _required(body, "rounds")
        if not isinstance(rounds, list) or len(rounds) != 3:
            raise MissingEvidence("Exactly three independent post-restart Telegraf rounds are required.")
        observed_at = _time(record["observed_at"])
        maximum_age = _required(self.evidence, "process_sample_max_age_s")
        if not _finite(maximum_age) or maximum_age <= 0:
            raise ValueError("Invalid independent Telegraf freshness maximum.")
        stamps = []
        for index, row in enumerate(rounds):
            stamp = timestamp_ns(_required(row, "read_at"))
            read_at = _time(row["read_at"])
            self.check(f"independent_telegraf_round_{index + 1}",
                restarted_at <= read_at <= observed_at and 0 <= observed_at - read_at <= maximum_age
                and type(row.get("collection_timestamp_ns")) is int and row["collection_timestamp_ns"] == stamp
                and all(_finite(row.get(key)) for key in expected)
                and all((row.get("status_codes") or {}).get(key) in {"OK (0x0)", "The operation succeeded. StatusGood (0x0)"} for key in expected)
                and all(type(row.get(key)) is int and row[key] >= 0 for key in ("invalid_message_count", "reconnect_count")),
                "Raw five-node values and Good quality share one fresh post-restart collection timestamp.")
            stamps.append(stamp)
        self.check("independent_telegraf_consecutive_rounds",
            all(0 < later - earlier <= 2_500_000_000 for earlier, later in zip(stamps, stamps[1:]))
            and all(row[key] == rounds[0][key] for row in rounds for key in ("invalid_message_count", "reconnect_count")),
            "Three distinct consecutive one-second collections have no invalid-message or reconnect increase.")
        final_status_at = _time(self.get("controller", "after")["observed_at"])
        self.check("independent_telegraf_brackets_final_hold",
                   0 <= final_status_at - observed_at <= maximum_age,
                   "The final nonrunning Controller capture immediately follows the independent fresh data proof.")


def evaluate(case: str, evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    """Evaluate one *captured* case; never converts unit fixtures to live proof.

    PASS means all narrow evidence checks passed. Missing capture -> INCOMPLETE;
    no captures -> NOT RUN; contradictory/malformed data -> FAIL. Full fault
    injection, restore operations and four-stage/hardware acceptance are separate.
    """
    if case not in CASES:
        raise ValueError(f"Unknown fault case: {case}")
    result: dict[str, Any] = {"case": case, "status": "NOT RUN", "checks": {},
        "scope": "Supplied evidence only; this evaluator performs no actions and does not authenticate provenance.",
        "hardware_and_camera": "NOT RUN"}
    observed_violations = (evidence or {}).get("safety_violations")
    if observed_violations:
        result["status"] = "FAIL"
        result["checks"]["supplied_safety_violations"] = {"status": "FAIL",
            "detail": "Captured safety violations remain failures regardless of missing later proof.",
            "observations": observed_violations}
    if not evidence or not evidence.get("captures"):
        return result
    review = None
    try:
        json.dumps(evidence, allow_nan=False)
        review = _Review(case, evidence)
        if case in {"stale_process", "missing_node", "broker_disconnect"}:
            review.process_loss()
        elif case == "broker_availability":
            review.broker_availability()
        elif case == "read_only_setpoint":
            review.readonly()
        elif case == "duplicate_command":
            review.duplicate()
        elif case == "growth_interruption":
            review.growth()
        else:
            review.restart()
        result["status"] = "PASS"
    except MissingEvidence as exc:
        result.update(status="INCOMPLETE", reason=str(exc))
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        result.update(status="FAIL", reason=f"Invalid evidence: {type(exc).__name__}: {exc}")
    if review is not None:
        result["checks"].update(review.checks)
        if any(item["status"] == "FAIL" for item in review.checks.values()):
            result["status"] = "FAIL"
    if observed_violations:
        result["status"] = "FAIL"
    return result


__all__ = ["CASES", "evaluate"]
