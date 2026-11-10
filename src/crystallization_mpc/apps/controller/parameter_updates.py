"""Translate the existing wire messages to GUI map assignments.

Wire receipts describe delivery/assignment. They do not create a numerical
transaction, trigger a filter refresh, or hold the source control loop.
"""
from __future__ import annotations
import copy
from typing import Mapping
from crystallization_mpc.messaging.parameter_policy import parameter_defaults
from crystallization_mpc.messaging.run_parameters import PARAMETERS_PREPARE, PARAMETERS_COMMIT, PARAMETERS_ABORT
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
        return self.parameters.get(key, default)

    @staticmethod
    def _complete_startup_parameters(parameters, sections):
        result = copy.deepcopy(dict(parameters))
        if isinstance(sections, Mapping):
            for section in ('shared', 'controller'):
                for key, value in sections.get(section, {}).items():
                    result.setdefault(key, copy.deepcopy(value))
        return result

    def _reset_run_parameters(self, sections=None, *, parameters=None):
        self.run_parameter_startup = parameter_defaults()
        if isinstance(sections, Mapping):
            for section, values in sections.items():
                self.run_parameter_startup.setdefault(section, {}).update(copy.deepcopy(values))
        self.run_parameter_values = copy.deepcopy(self.run_parameter_startup)
        self.run_parameter_pending = None
        self.run_parameter_last_result = None
        self._active_parameters = copy.deepcopy(parameters or self.parameters)

    def _actual_run_parameter_values(self):
        values = copy.deepcopy(self.run_parameter_values)
        actual = self.adapter.runtime_parameter_values() if self._adapter_configured else None
        if isinstance(actual, Mapping):
            for section, fields in actual.items():
                values.setdefault(section, {}).update(copy.deepcopy(fields))
        return values

    def _online_parameter_document(self):
        return {'schema_version': 1, 'startup_values': self.run_parameter_startup,
                'values': self._actual_run_parameter_values(), 'pending': self.run_parameter_pending,
                'last_result': self.run_parameter_last_result}

    def _run_parameter_status(self):
        return {'supported': self._adapter_configured, 'revision': self.runtime_revision,
                'measurement_parameter_revision': self.measurement_parameter_revision,
                'startup_values': copy.deepcopy(self.run_parameter_startup),
                'values': self._actual_run_parameter_values(),
                'pending': copy.deepcopy(self.run_parameter_pending),
                'last_result': copy.deepcopy(self.run_parameter_last_result)}

    def _parameter_tick_patch(self):
        return None

    def _revalidate_prepared_parameters(self, tick, process_state):
        return tick

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
        if payload.get('run_id') != self.current_run_id:
            raise ValueError('Parameter message belongs to another experiment.')
        event_id = payload.get('event_id')
        previous = self.runtime_events.get(event_id)
        if previous:
            return {**copy.deepcopy(previous['result']), 'duplicate': True}
        changes = payload.get('changes', {})
        if not isinstance(changes, Mapping):
            raise TypeError('Parameter changes must be an object.')
        values = {}
        for section in ('shared', 'controller'):
            values.update(copy.deepcopy(changes.get(section, {})))
        # Dotted names retain their MATLAB workspace/params distinction.
        result = self.submit_operator_map(values, event_id=event_id,
            command=payload, parameter_changes=changes, kind='parameters')
        self.run_parameter_last_result = copy.deepcopy(result)
        return result

    def _accept_parameter_result(self, payload):
        pending = self.run_parameter_pending
        if pending is None or payload.get('event_id') != pending['command'].get('event_id'):
            return {'accepted': True, 'kind': 'parameter_observation', 'controls_changed': False}
        if payload.get('run_id') != self.current_run_id:
            raise ValueError('Parameter acknowledgement belongs to another experiment.')
        status = payload.get('status')
        if status == 'ready':
            try:
                self._send_parameter_phase(pending, 'commit')
                pending['phase'] = 'confirming'
            except Exception as exc:
                pending['transport_error'] = str(exc)
        elif status in ('committed', 'applied'):
            for section, fields in pending['changes'].items():
                self.run_parameter_values.setdefault(section, {}).update(copy.deepcopy(fields))
            self.measurement_parameter_revision = pending['revision']
            entry = self.runtime_events.get(pending['command']['event_id'])
            if entry:
                entry['result'].update(status='applied', phase='applied', gsensor_applied=True)
                self.run_parameter_last_result = copy.deepcopy(entry['result'])
                self.runtime_last_result = copy.deepcopy(entry['result'])
            self.run_parameter_pending = None
        elif status in ('rejected', 'failed', 'aborted'):
            pending['phase'] = 'failed'
            pending['error'] = payload.get('reason') or payload.get('error')
            self.run_parameter_last_result = {**copy.deepcopy(pending), 'status': 'rejected',
                'controller_applied': True, 'refreshed': False}
            self.run_parameter_pending = None
        return {'accepted': True, 'kind': 'parameter_observation', 'controls_changed': False}

    def _reject_run_parameters(self, reason):
        # Stop cancels undelivered assignments; completed assignments are never
        # rolled back because another process did not confirm its own update.
        self.run_parameter_pending = None
