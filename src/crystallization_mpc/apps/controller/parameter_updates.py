"""Controller-owned coordination of run-only parameter transactions.

Preparation is reversible. A durable Controller commit is not silently undone
when an acknowledgement from another process is lost. The same event is retried
and outputs remain held until the measurement process confirms its commit.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping

from crystallization_mpc.apps.controller.adapter import ControllerAdapter
from crystallization_mpc.messaging.parameter_policy import (
    parameter_defaults, parameter_policies, validate_parameter_changes,
    measurement_parameter_changes,
)
from crystallization_mpc.messaging.run_parameters import (
    RunParametersUpdatePayload, PARAMETERS_PREPARE, PARAMETERS_COMMIT, PARAMETERS_ABORT,
)
from crystallization_mpc.messaging.schema import build_envelope, utc_ts
from crystallization_mpc.messaging.routing import route


class OnlineParametersMixin:
    def _initialize_online_parameters(self, publisher=None):
        self._parameter_publisher = publisher
        self.run_parameter_startup = parameter_defaults()
        self.run_parameter_values = copy.deepcopy(self.run_parameter_startup)
        self.run_parameter_pending = None
        self.run_parameter_last_result = None
        self.measurement_parameter_revision = 0
        self._active_parameters = {}
        self._run_elapsed_s = 0.0

    def _active_parameter(self, key, default=None):
        return self._active_parameters.get(key, self.parameters.get(key, default))

    @staticmethod
    def _complete_startup_parameters(parameters, sections):
        """Normalize once without losing standby-target values on old flat wire.

        Central's historical flat message contains active-target aliases only;
        the full frozen raw sections retain both targets. Never substitute
        defaults for the latter or overwrite an explicitly conflicting value.
        """
        result = copy.deepcopy(parameters)
        if sections is None:
            return result
        defaults = parameter_defaults()
        if not isinstance(sections, Mapping) or set(sections) != set(defaults):
            raise ValueError("parameter_sections must contain the three parameter sections.")
        for section, fields in defaults.items():
            supplied = sections[section]
            if not isinstance(supplied, Mapping) or set(supplied) != set(fields):
                raise ValueError("Startup parameter section does not match the parameter catalog.")
            if section != "gsensor":
                for key, value in supplied.items():
                    if key in result and result[key] != value:
                        raise ValueError("Startup parameter sections disagree with the immutable Controller snapshot.")
                    result[key] = copy.deepcopy(value)
        return result

    def _reset_run_parameters(self, sections=None, *, parameters=None):
        startup_parameters = self.parameters if parameters is None else parameters
        values = parameter_defaults()
        if sections is not None:
            if not isinstance(sections, Mapping) or set(sections) != set(values):
                raise ValueError("parameter_sections must contain the three parameter sections.")
            for section, fields in values.items():
                supplied = sections[section]
                if not isinstance(supplied, Mapping) or set(supplied) != set(fields):
                    raise ValueError("Startup parameter section does not match the parameter catalog.")
                values[section] = copy.deepcopy(dict(supplied))
        for section in ("shared", "controller"):
            for key in values[section]:
                if key in startup_parameters:
                    if sections is not None and values[section][key] != startup_parameters[key]:
                        raise ValueError("Startup parameter sections disagree with the immutable Controller snapshot.")
                    values[section][key] = copy.deepcopy(startup_parameters[key])
        self.run_parameter_startup = values
        self.run_parameter_values = copy.deepcopy(values)
        self.run_parameter_pending = None
        self.run_parameter_last_result = None
        self.measurement_parameter_revision = 0
        self._active_parameters = copy.deepcopy(startup_parameters)
        self._run_elapsed_s = 0.0

    def _actual_run_parameter_values(self):
        values = copy.deepcopy(self.run_parameter_values)
        actual = (self.adapter.runtime_parameter_values()
                  if self.adapter.runtime_configuration() is not None else None)
        if isinstance(actual, Mapping):
            for section in ("shared", "controller"):
                for key, value in actual.get(section, {}).items():
                    if key in values[section]:
                        values[section][key] = copy.deepcopy(value)
        # The existing GSensor alignment UI may change the confirmed method
        # independently. Observe its validated status without rewriting the
        # immutable startup snapshot or inventing a parameter transaction.
        if self.gsensor_alignment_method is not None:
            values["gsensor"]["alignment_method"] = self.gsensor_alignment_method
        return values

    def _online_parameter_document(self):
        return {"schema_version": 1, "startup_values": copy.deepcopy(self.run_parameter_startup),
                "values": copy.deepcopy(self.run_parameter_values),
                "pending": copy.deepcopy(self.run_parameter_pending),
                "last_result": copy.deepcopy(self.run_parameter_last_result),
                "measurement_revision": self.measurement_parameter_revision,
                "elapsed_s": self._run_elapsed_s}

    def _restore_online_parameter_document(self, document):
        saved = document.get("run_parameters")
        if saved is None:
            self._reset_run_parameters()
            last = document.get("control_output", {}).get("last") or {}
            self._run_elapsed_s = float(last.get("elapsed_s", self.control_tick_count * float(self.parameters.get("dt", 5))))
            return
        if not isinstance(saved, dict) or saved.get("schema_version") != 1:
            raise ValueError("Invalid run parameter checkpoint.")
        self._reset_run_parameters(saved.get("startup_values"))
        values = saved.get("values")
        if not isinstance(values, dict) or set(values) != set(self.run_parameter_values):
            raise ValueError("Run parameter values are missing.")
        # Only confirmed Controller commits may alter the run-only overlay.
        expected = copy.deepcopy(self.run_parameter_startup)
        entries = sorted((item for item in document.get("runtime_controls", {}).get("events", {}).values()
                          if "experiment_started_at" in item.get("command", {})
                          and item.get("result", {}).get("controller_applied") is True),
                         key=lambda item: item["result"]["revision"])
        for entry in entries:
            event = RunParametersUpdatePayload.from_mapping(entry["command"])
            if event.run_id != self.current_run_id or event.experiment_started_at != self.started_at:
                raise ValueError("Run parameter journal belongs to another experiment.")
            applied_changes = validate_parameter_changes(entry["result"].get("parameter_changes", {}))
            if not applied_changes or any(
                    key not in event.changes.get(section, {}) or event.changes[section][key] != value
                    for section, changes in applied_changes.items() for key, value in changes.items()):
                raise ValueError("Committed parameter changes disagree with the operator request.")
            for section, changes in applied_changes.items():
                expected[section].update(changes)
        if values != expected:
            raise ValueError("Run parameter overlay disagrees with its committed journal.")
        self.run_parameter_values = copy.deepcopy(values)
        for section in ("shared", "controller"):
            self._active_parameters.update(values[section])
        self.run_parameter_pending = copy.deepcopy(saved.get("pending"))
        self.run_parameter_last_result = copy.deepcopy(saved.get("last_result"))
        revision = saved.get("measurement_revision", 0)
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid measurement parameter revision.")
        self.measurement_parameter_revision = revision
        expected_measurement_revision = max(
            (entry["result"]["revision"] for entry in entries
             if measurement_parameter_changes(entry["result"].get("parameter_changes", {}))), default=0)
        if revision != expected_measurement_revision:
            raise ValueError("Measurement parameter revision disagrees with its committed journal.")
        elapsed = saved.get("elapsed_s", 0.0)
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not 0 <= elapsed < float("inf"):
            raise ValueError("Invalid accumulated control time.")
        self._run_elapsed_s = float(elapsed)
        numerical = (document.get("adapter") or {}).get("state") or {}
        if document.get("state") in {"running", "recovery_required"} and "elapsed_s" in numerical:
            numerical_elapsed = numerical["elapsed_s"]
            frame = numerical.get("frame_index")
            last_output = document.get("control_output", {}).get("last") or {}
            # A retained failed I/O attempt may own a service tick without
            # advancing (or after rolling back) the numerical algorithm.
            aborted_attempt = (
                frame == self.control_tick_count - 1
                and last_output.get("tick_seq") == self.control_tick_count
                and last_output.get("result", {}).get("valid") is False
                and last_output.get("elapsed_s") == elapsed
                and math.isclose(float(numerical_elapsed) + float(last_output["controller_dt_s"]),
                                 elapsed, rel_tol=0, abs_tol=1e-9))
            if (isinstance(numerical_elapsed, bool) or not isinstance(numerical_elapsed, (int, float))
                    or not math.isfinite(numerical_elapsed)
                    or (not aborted_attempt and not math.isclose(numerical_elapsed, elapsed, rel_tol=0, abs_tol=1e-9))):
                raise ValueError("Accumulated control time disagrees with the algorithm checkpoint.")
        pending = self.run_parameter_pending
        if pending is not None:
            event = RunParametersUpdatePayload.from_mapping(pending["command"])
            entry = document["runtime_controls"]["events"].get(event.event_id)
            if (entry is None or entry["command"] != event.to_dict()
                    or entry["result"].get("status") != "pending"
                    or pending.get("phase") not in {"preparing", "ready", "confirming"}):
                raise ValueError("Invalid pending run parameter transaction.")
            if (pending.get("changes") != entry["result"].get("parameter_changes")
                    or type(pending.get("controller_applied")) is not bool
                    or pending["controller_applied"] != (pending["phase"] == "confirming")
                    or event.expected_revision + int(pending["controller_applied"]) != self.runtime_revision
                    or bool(pending.get("requires_gsensor")) != bool(
                        measurement_parameter_changes(pending["changes"]) or pending["changes"].get("gsensor"))
                    or pending.get("measurement_changed") != bool(measurement_parameter_changes(pending["changes"]))):
                raise ValueError("Pending parameter decision disagrees with its journal.")
            if (not pending["changes"] or any(
                    key not in event.changes.get(section, {}) or event.changes[section][key] != value
                    for section, changes in pending["changes"].items() for key, value in changes.items())
                    or type(pending.get("requires_gsensor")) is not bool
                    or type(pending.get("measurement_changed")) is not bool
                    or (not pending["requires_gsensor"] and pending["phase"] != "ready")):
                raise ValueError("Pending parameter changes disagree with the operator request.")
        if document.get("state") in {"running", "recovery_required"}:
            applied_ids = set((self.adapter.runtime_parameter_status() or {}).get("applied_event_ids", []))
            if any(entry["command"]["event_id"] not in applied_ids for entry in entries):
                raise ValueError("Run parameter journal has no matching algorithm application receipt.")

    def _run_parameter_status(self):
        complete_startup = self._configured_parameter_sections is not None
        supported = self.adapter.runtime_parameter_values() is not None and complete_startup
        algorithm_status = self.adapter.runtime_parameter_status() or {}
        return {"supported": supported, "revision": self.runtime_revision,
                "startup_sections_available": complete_startup,
                "unavailable_reason": None if complete_startup else
                    "This legacy run has no complete raw startup sections. End it and start a new run to use online parameters.",
                "values": self._actual_run_parameter_values() if complete_startup else None,
                "derived_values": copy.deepcopy(algorithm_status.get("derived_values")),
                "startup_values": copy.deepcopy(self.run_parameter_startup) if complete_startup else None,
                "policies": parameter_policies(),
                "pending": copy.deepcopy(self.run_parameter_pending),
                "last_result": copy.deepcopy(self.run_parameter_last_result),
                "measurement_revision": self.measurement_parameter_revision,
                "events": [copy.deepcopy(item["result"]) for item in self.runtime_events.values()
                           if "experiment_started_at" in item["command"]][-50:]}

    def _send_parameter_phase(self, pending, phase):
        publisher = self._parameter_publisher
        transient = publisher is None
        if transient:
            from crystallization_mpc.apps.gsensor.status_publisher import RabbitStatusPublisher
            publisher = RabbitStatusPublisher(
                url=self.settings.rabbit_url, exchange=self.settings.rabbit_exchange,
                destination_queue="gsensor.in", destination_role="gsensor")
        command = pending["command"]
        payload = {"run_id": command["run_id"], "experiment_started_at": command["experiment_started_at"],
                   "event_id": command["event_id"], "revision": command["expected_revision"] + 1,
                   "changes": copy.deepcopy(pending["changes"])}
        name = {"prepare": PARAMETERS_PREPARE, "commit": PARAMETERS_COMMIT, "abort": PARAMETERS_ABORT}[phase]
        envelope = build_envelope("controller", "gsensor", "command", name,
                                  payload["revision"], payload, command["event_id"])
        try:
            # Prepare/retry runs on the consumer thread; commit runs on the
            # scheduler thread. A Pika BlockingConnection must never migrate
            # between them, even under the service's serialization lock.
            publisher.publish(route("controller", "gsensor"), envelope)
        finally:
            if transient:
                publisher.close()

    def _update_run_parameters(self, payload):
        event = RunParametersUpdatePayload.from_mapping(payload)
        with self._lock:
            if event.run_id != self.current_run_id or event.experiment_started_at != self.started_at:
                raise ValueError("Run parameter experiment identity does not match.")
            command = event.to_dict()
            prior = self.runtime_events.get(event.event_id)
            if prior:
                if prior["command"] != command:
                    raise ValueError("Parameter event_id was reused with different content.")
                pending = self.run_parameter_pending
                if pending and pending["command"]["event_id"] == event.event_id and pending["requires_gsensor"]:
                    try:
                        self._send_parameter_phase(pending, "commit" if pending["phase"] == "confirming" else "prepare")
                    except Exception as exc:
                        pending["transport_error"] = type(exc).__name__
                return {**copy.deepcopy(prior["result"]), "duplicate": True}
            result = {"kind": "runtime", "parameter_update": True, "event_id": event.event_id,
                      "run_id": event.run_id, "requested_at": event.requested_at,
                      "processed_at": utc_ts(), "revision": self.runtime_revision,
                      "configuration": copy.deepcopy(self.runtime_configuration_current), "refreshed": False}
            try:
                if self.state.value != "running":
                    raise ValueError("Controller is not running an experiment.")
                if event.expected_revision != self.runtime_revision:
                    raise ValueError("Runtime parameter revision conflict.")
                if self.runtime_pending is not None or self.run_parameter_pending is not None:
                    raise ValueError("Another runtime parameter update is pending.")
                if self.adapter.runtime_parameter_values() is None:
                    raise ValueError("Controller adapter does not support online parameters.")
                if self._configured_parameter_sections is None:
                    raise ValueError("This legacy run has no complete raw startup sections; end it and start a new run to use online parameters.")
                if (not self.adapter.supports_runtime_refresh()
                        or type(self.adapter).rollback_runtime_refresh is ControllerAdapter.rollback_runtime_refresh
                        or type(self.adapter).commit_runtime_refresh is ControllerAdapter.commit_runtime_refresh):
                    raise ValueError("Controller adapter does not support atomic online parameter updates.")
                current = self._actual_run_parameter_values()
                changes = validate_parameter_changes(event.changes, current=current)
                changes = {section: {key: value for key, value in fields.items()
                                     if current[section].get(key) != value}
                           for section, fields in changes.items()}
                changes = {section: fields for section, fields in changes.items() if fields}
                if changes:
                    candidate = self.adapter.validate_parameter_update(changes)
                    if self.settings.process_enabled:
                        low = float(candidate["T_j_min"])
                        high = float(candidate["T_j_max"])
                        if low < self.settings.process_jacket_min_K or high > self.settings.process_jacket_max_K:
                            raise ValueError("Controller jacket bounds exceed the device Writer envelope.")
                    self._validate_parameter_hold_bounds(candidate, changes)
            except (ValueError, TypeError, KeyError, NotImplementedError, RuntimeError) as exc:
                result.update(accepted=False, status="rejected", reason=str(exc), no_change=False)
            else:
                if not changes:
                    result.update(accepted=True, status="applied", no_change=True,
                                  effective_tick=self.control_tick_count, applied_at=utc_ts())
                else:
                    gs_changes = measurement_parameter_changes(changes)
                    requires = bool(gs_changes or changes.get("gsensor"))
                    pending = {"command": command, "changes": changes,
                               "requires_gsensor": requires, "measurement_changed": bool(gs_changes),
                               "phase": "preparing" if requires else "ready", "controller_applied": False}
                    self.run_parameter_pending = pending
                    result.update(accepted=True, status="pending", phase=pending["phase"], no_change=False,
                                  parameter_changes=copy.deepcopy(changes), controller_applied=False)
            self.runtime_events[event.event_id] = {"command": command, "result": result}
            self.run_parameter_last_result = copy.deepcopy(result)
            self.runtime_last_result = copy.deepcopy(result)
            persisted = self._try_persist_state_locked()
            if not persisted:
                self._reject_run_parameters("Parameter request checkpoint could not be saved.")
                result.update(accepted=False, status="rejected", phase="failed",
                              reason="Parameter request checkpoint could not be saved.", refreshed=False)
                result.pop("applied_at", None)
                self.runtime_events[event.event_id]["result"] = copy.deepcopy(result)
                self.run_parameter_last_result = copy.deepcopy(result)
                self.runtime_last_result = copy.deepcopy(result)
                self._persistence_halted = True
                self.state = type(self.state).ERROR
                self._next_tick_monotonic = None
                return copy.deepcopy(self.runtime_events[event.event_id]["result"])
            if result["status"] == "pending" and self.run_parameter_pending["requires_gsensor"]:
                try:
                    self._send_parameter_phase(self.run_parameter_pending, "prepare")
                except Exception as exc:
                    self.run_parameter_pending["transport_error"] = type(exc).__name__
            self._archive_runtime_event(command, result)
            return copy.deepcopy(result)

    def _parameter_tick_patch(self):
        pending = self.run_parameter_pending
        if pending is None or pending["phase"] != "ready":
            return None
        return {"event_id": pending["command"]["event_id"], "changes": {},
                "parameter_changes": copy.deepcopy(pending["changes"]),
                "measurement_changed": pending["measurement_changed"]}

    def _parameter_confirmation_holds_control(self):
        pending = self.run_parameter_pending
        if pending is None or pending["phase"] != "confirming":
            return False
        event = self.runtime_events[pending["command"]["event_id"]]["result"]
        # A metadata-only GSensor acknowledgement must not make an otherwise
        # unused parameter affect the running control calculation.
        return event.get("refreshed") is True

    def _validate_parameter_hold_bounds(self, candidate, changes, process_state=None):
        if not ({"T_j_min", "T_j_max"} & set(changes.get("controller", {}))):
            return
        if process_state is not None:
            held = process_state.T_j_set
        elif self.settings.process_enabled and self.last_process_state is not None:
            held = self.last_process_state["T_j_set"]
        else:
            held = (self.last_control_output or {}).get("result", {}).get("T_j_set")
            if held is None:
                held = self._active_parameter("T_j_set_init", 315.15)
        if not float(candidate["T_j_min"]) <= float(held) <= float(candidate["T_j_max"]):
            raise ValueError("New bounds exclude the jacket command that must be held during refresh.")

    def _revalidate_prepared_parameters(self, tick, process_state):
        """A held command can change while a participant is preparing.

        Reject only this candidate before any numerical mutation. The ordinary
        cycle still runs once with the old configuration and the fresh input.
        """
        pending = self.run_parameter_pending
        if pending is None or pending["phase"] != "ready":
            return tick
        try:
            candidate = self.adapter.validate_parameter_update(pending["changes"])
            self._validate_parameter_hold_bounds(candidate, pending["changes"], process_state)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            self._reject_run_parameters(f"Prepared parameters no longer apply: {exc}")
            from dataclasses import replace
            return replace(tick, runtime_update=None)
        return tick

    def _complete_run_parameter_tick(self, tick, output):
        pending = self.run_parameter_pending
        if pending is None or pending["phase"] != "ready":
            return
        event_id = pending["command"]["event_id"]
        receipt = (self.adapter.runtime_parameter_status() or {}).get("last_event") or {}
        if (output is None or not output.valid or receipt.get("event_id") != event_id
                or receipt.get("effective_tick") != tick.tick_seq
                or receipt.get("elapsed_s") != tick.elapsed_s
                or receipt.get("parameter_changes") != pending["changes"]):
            raise RuntimeError("Algorithm did not produce a parameter application receipt.")
        previous = (copy.deepcopy(self.run_parameter_values), copy.deepcopy(self._active_parameters),
                    self.runtime_revision, copy.deepcopy(self.runtime_configuration_current),
                    copy.deepcopy(self.runtime_refresh), self.measurement_parameter_revision)
        refresh = self.adapter.refresh_status() or {}
        refreshed = receipt.get("refreshed") is True
        if (type(receipt.get("refreshed")) is not bool
                or refresh.get("count", 0) != previous[4]["count"] + int(refreshed)
                or refresh.get("segment_id", 0) != previous[4]["segment_id"] + int(refreshed)):
            raise RuntimeError("Parameter refresh count disagrees with algorithm receipt.")
        actual = self.adapter.runtime_parameter_values() or {}
        if any(actual.get(section, {}).get(key) != value
               for section, fields in pending["changes"].items() if section != "gsensor"
               for key, value in fields.items()):
            raise RuntimeError("Algorithm did not apply the requested parameter values.")
        configuration = dict(self.adapter.runtime_configuration())
        for section, changes in pending["changes"].items():
            self.run_parameter_values[section].update(copy.deepcopy(changes))
            if section in ("shared", "controller"):
                self._active_parameters.update(copy.deepcopy(changes))
        self.runtime_revision += 1
        self.runtime_configuration_current = configuration
        if refreshed:
            self.runtime_refresh = {"state": "completed", "event_id": event_id,
                "count": refresh["count"], "segment_id": refresh["segment_id"],
                "effective_tick": tick.tick_seq, "elapsed_s": tick.elapsed_s,
                "reason": "runtime_parameters_changed"}
        if pending["measurement_changed"]:
            self.measurement_parameter_revision = self.runtime_revision
            self._invalidate_growth_input_locked(reason="parameter_reconfiguration", frame_seq=self.last_frame_seq,
                                                 occurred_at=utc_ts())
        pending.update(controller_applied=True, phase="confirming", effective_tick=tick.tick_seq)
        entry = self.runtime_events[event_id]
        entry["result"].update(revision=self.runtime_revision, effective_tick=tick.tick_seq,
            elapsed_s=tick.elapsed_s, controller_applied=True, refreshed=refreshed,
            refresh_count=refresh.get("count", 0), segment_id=refresh.get("segment_id", 0),
            configuration=copy.deepcopy(self.runtime_configuration_current),
            parameter_values=self._actual_run_parameter_values(), phase="confirming")
        if not pending["requires_gsensor"]:
            entry["result"].update(status="applied", phase="applied", applied_at=utc_ts())
            self.run_parameter_pending = None
        self.run_parameter_last_result = copy.deepcopy(entry["result"])
        self.runtime_last_result = copy.deepcopy(entry["result"])
        if not self._try_persist_state_locked():
            (self.run_parameter_values, self._active_parameters, self.runtime_revision,
             self.runtime_configuration_current, self.runtime_refresh, self.measurement_parameter_revision) = previous
            self.run_parameter_pending = pending
            pending["controller_applied"] = False
            entry["result"].update(revision=self.runtime_revision, controller_applied=False, refreshed=False,
                                    configuration=copy.deepcopy(self.runtime_configuration_current))
            entry["result"].pop("applied_at", None)
            entry["result"].pop("parameter_values", None)
            self._persistence_halted = True
            self._reject_run_parameters("Parameter application checkpoint failed.")
            raise RuntimeError("Parameter application checkpoint failed.")
        self.adapter.commit_runtime_refresh(event_id)
        if pending["requires_gsensor"]:
            try:
                self._send_parameter_phase(pending, "commit")
            except Exception as exc:
                pending["transport_error"] = type(exc).__name__
        self._archive_runtime_event(entry["command"], entry["result"])

    def _accept_parameter_result(self, payload):
        with self._lock:
            pending = self.run_parameter_pending
            if pending is None:
                entry = self.runtime_events.get(payload.get("event_id"))
                if entry and entry["result"].get("parameter_update"):
                    command = entry["command"]
                    self._validate_parameter_ack_identity(payload, command)
                    if entry["result"].get("status") == "applied" and payload.get("phase") == "applied":
                        self._validate_parameter_ack_values(payload, entry["result"].get("parameter_changes", {}),
                                                           entry["result"].get("measurement_revision", 0))
                        return {"accepted": True, "kind": "parameter_ack", "duplicate": True,
                                "event_id": command["event_id"]}
                    if (entry["result"].get("status") == "rejected"
                            and entry["result"].get("controller_applied") is not True
                            and payload.get("phase") == "aborted"):
                        return {"accepted": True, "kind": "parameter_ack", "aborted": True,
                                "event_id": command["event_id"]}
                raise ValueError("No matching parameter update is awaiting GSensor confirmation.")
            command = pending["command"]
            self._validate_parameter_ack_identity(payload, command)
            phase = payload.get("phase")
            if phase in {"prepared", "applied"}:
                self._validate_parameter_ack_values(payload, pending["changes"],
                    command["expected_revision"] + 1 if pending["measurement_changed"] else self.measurement_parameter_revision)
            if phase == "rejected":
                self._reject_run_parameters(str(payload.get("error") or "GSensor rejected the parameter update."))
            elif phase == "prepared":
                if pending["phase"] == "preparing":
                    pending["phase"] = "ready"
                    self.runtime_events[command["event_id"]]["result"]["phase"] = "ready"
            elif phase == "applied":
                if not pending["controller_applied"]:
                    raise ValueError("GSensor applied a parameter change before the Controller decision.")
                if pending["measurement_changed"]:
                    frame = payload["effective_after_frame_seq"]
                    self.gsensor_sample_floor_frame_seq = max(self.gsensor_sample_floor_frame_seq, frame)
                    if self.last_valid_sample is not None and self.last_valid_sample.frame_seq <= frame:
                        self._invalidate_growth_input_locked(reason="waiting_for_fresh_valid_sample", frame_seq=frame)
                method = payload.get("values", {}).get("gsensor", {}).get("alignment_method")
                if method is not None:
                    self.gsensor_alignment_method = method
                result = self.runtime_events[command["event_id"]]["result"]
                result.update(status="applied", phase="applied", applied_at=utc_ts(),
                              effective_after_frame_seq=payload["effective_after_frame_seq"],
                              measurement_revision=self.measurement_parameter_revision,
                              parameter_values=self._actual_run_parameter_values())
                self.run_parameter_pending = None
            else:
                raise ValueError("Unsupported GSensor parameter acknowledgement phase.")
            result = self.runtime_events[command["event_id"]]["result"]
            self.run_parameter_last_result = copy.deepcopy(result)
            self.runtime_last_result = copy.deepcopy(result)
            persisted = self._try_persist_state_locked()
            if not persisted:
                if pending.get("controller_applied"):
                    result.update(status="pending", phase="confirming", reason="GSensor committed; checkpoint confirmation failed.")
                    result.pop("applied_at", None)
                    self.run_parameter_pending = pending
                    self.run_parameter_last_result = copy.deepcopy(result)
                    self.runtime_last_result = copy.deepcopy(result)
                else:
                    self._reject_run_parameters("GSensor preparation checkpoint failed.")
                self._persistence_halted = True
                self.state = type(self.state).ERROR
                self._next_tick_monotonic = None
            self._archive_runtime_event(command, result)
            return {"accepted": bool(persisted), "kind": "parameter_ack", "event_id": command["event_id"],
                    **({"reason": self.runtime_last_result.get("reason") or "Parameter acknowledgement checkpoint failed."}
                       if not persisted else {})}

    def _validate_parameter_ack_identity(self, payload, command):
        for key, expected in (("run_id", self.current_run_id), ("experiment_started_at", self.started_at),
                              ("event_id", command["event_id"]), ("revision", command["expected_revision"] + 1)):
            if type(payload.get(key)) is not type(expected) or payload[key] != expected:
                raise ValueError(f"GSensor parameter acknowledgement has the wrong {key}.")

    @staticmethod
    def _validate_parameter_ack_values(payload, changes, measurement_revision):
        expected = {section: copy.deepcopy(values) for section, values in changes.items()
                    if section in {"shared", "gsensor"} and values}
        if payload.get("values") != expected:
            raise ValueError("GSensor acknowledged different parameter values.")
        if type(payload.get("measurement_revision")) is not int or payload["measurement_revision"] != measurement_revision:
            raise ValueError("GSensor acknowledged a different measurement parameter revision.")
        frame = payload.get("effective_after_frame_seq")
        if type(frame) is not int or frame < 0:
            raise ValueError("GSensor acknowledgement has no valid effective frame.")

    def _reject_run_parameters(self, reason):
        pending = self.run_parameter_pending
        if pending is None:
            return
        event_id = pending["command"]["event_id"]
        committed = pending.get("controller_applied") is True
        if committed:
            # Neither an applied device command nor a new measurement segment
            # can be made old again by changing metadata on a failed request.
            self.state = type(self.state).ERROR
            self._next_tick_monotonic = None
            reason = (f"{reason} Controller parameters were already committed. "
                      "This is a partial update, not a rollback; end this experiment before starting a new run.")
        else:
            try:
                self.adapter.rollback_runtime_refresh(event_id)
            except Exception as exc:
                reason = f"{reason} Numerical rollback failed: {type(exc).__name__}"
                self._persistence_halted = True
                self.state = type(self.state).ERROR
                self._next_tick_monotonic = None
            if pending["requires_gsensor"]:
                try:
                    self._send_parameter_phase(pending, "abort")
                except Exception:
                    pass
        result = self.runtime_events[event_id]["result"]
        result.update(status="rejected", accepted=False, reason=reason, phase="failed",
                      controller_applied=committed, processed_at=utc_ts())
        result.pop("applied_at", None)
        self.run_parameter_last_result = copy.deepcopy(result)
        self.runtime_last_result = copy.deepcopy(result)
        self.run_parameter_pending = None
        self._archive_runtime_event(pending["command"], result)
