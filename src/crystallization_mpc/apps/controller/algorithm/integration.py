"""Connect the crystallization algorithm to the Controller application."""

from __future__ import annotations

from typing import Any, Mapping

from crystallization_mpc.apps.controller.adapter import ControllerAdapter
from crystallization_mpc.apps.controller.diagnostics import ControllerDiagnosticsSnapshot
from crystallization_mpc.apps.controller.process import ProcessState
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.apps.controller.algorithm.controller import (
    CrystallizationController,
)


class CrystallizationControllerAdapter(ControllerAdapter):
    """Adapt the stateful control algorithm to the application interface."""

    def __init__(self, controller: CrystallizationController | None = None) -> None:
        # Keep one controller instance for the whole experiment so histories,
        # EKF state and integral terms persist across scheduler ticks.
        self.controller = controller or CrystallizationController()

    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        # Source receive_params assigns the supplied workspace.
        self.controller.configure(params, run_id)

    def start(self) -> None:
        # Algorithm initialization is owned by controller.py.
        self.controller.start()

    def assign_initial_scalar(self, name: str, value: Any, *, raw_units: bool = True) -> None:
        self.controller.assign_initial_scalar(name, value, raw_units=raw_units)

    def complete_initialization(self) -> None:
        self.controller.complete_initialization()

    def apply_modified(self, mapping: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.controller.apply_modified(mapping)

    def request_refresh(self) -> None:
        self.controller.request_refresh()

    def refresh_pending(self) -> bool:
        return self.controller.refresh_pending()

    def prepare_iteration(self) -> None:
        self.controller.prepare_iteration()

    def set_g_active(self, active: bool) -> None:
        self.controller.set_g_active(active)

    def workspace_snapshot(self) -> Mapping[str, Any]:
        return self.controller.workspace_snapshot()

    def initialize_process_state(self, process_state: ProcessState) -> None:
        self.controller.initialize_process_state(process_state)

    def record_activation_read_failure(self, error: Exception) -> None:
        self.controller.record_activation_read_failure(error)


    def record_read_failure(self, tick: ControllerTickInput, error: Exception | None = None) -> None:
        self.controller.record_read_failure(tick, error)



    def step(self, tick: ControllerTickInput) -> ControllerStepResult | None:
        if not isinstance(tick, ControllerTickInput):
            raise TypeError("step() requires the Controller-clock input.")
        return self.controller.step(tick)

    def stop(self) -> None:
        self.controller.stop()


    def set_adaptation(
        self,
        enabled: bool,
        mode: str,
        event: Mapping[str, Any] | None = None,
    ) -> None:
        self.controller.set_adaptation(enabled, mode, event)

    def runtime_configuration(self) -> Mapping[str, Any] | None:
        if not self.controller.configured:
            return None
        return self.controller.runtime_configuration()

    def update_runtime(self, changes: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.controller.update_runtime(changes)

    def supports_runtime_refresh(self) -> bool:
        return True

    def validate_runtime_update(self, changes: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.controller.validate_runtime_update(changes)

    def validate_parameter_update(self, changes: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.controller.validate_parameter_update(changes)

    def runtime_parameter_values(self) -> Mapping[str, Any]:
        return self.controller.runtime_parameter_values()

    def runtime_parameter_status(self) -> Mapping[str, Any] | None:
        return self.controller.runtime_parameter_status()

    def refresh_status(self) -> Mapping[str, Any]:
        return self.controller.refresh_status()



    def adaptation_status(self) -> Mapping[str, Any] | None:
        if not self.controller.configured:
            return None
        return self.controller.adaptation_status()

    def diagnostics_snapshot(self) -> ControllerDiagnosticsSnapshot | None:
        return self.controller.diagnostics_snapshot()

    def export_state(self) -> Mapping[str, Any] | None:
        return self.controller.export_state()



    def restore_state(self, params, run_id, state):
        return self.controller.restore_state(params, run_id, state)

    def restore_held_state(self, params, run_id, state):
        return self.restore_state(params, run_id, state)


__all__ = ["CrystallizationControllerAdapter"]
