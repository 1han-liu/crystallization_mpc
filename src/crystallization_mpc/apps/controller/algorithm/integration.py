"""Connect the crystallization algorithm to the Controller application."""

from __future__ import annotations

from typing import Any, Mapping

from crystallization_mpc.apps.controller.adapter import ControllerAdapter
from crystallization_mpc.apps.controller.process import ProcessState
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.apps.controller.algorithm.controller import (
    CrystallizationController,
)
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload


class CrystallizationControllerAdapter(ControllerAdapter):
    """Adapt the stateful control algorithm to the application interface."""

    def __init__(self, controller: CrystallizationController | None = None) -> None:
        # Keep one controller instance for the whole experiment so histories,
        # EKF state and integral terms persist across scheduler ticks.
        self.controller = controller or CrystallizationController()

    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        # Central supplies one immutable parameter snapshot per run.
        self.controller.configure(params, run_id)

    def start(self) -> None:
        # Algorithm initialization is owned by controller.py.
        self.controller.start()

    def step(
        self,
        sample: GrowthRateSamplePayload | ControllerTickInput,
        process_state: ProcessState | None = None,
    ) -> ControllerStepResult | None:
        # Execute one controller-clock cycle.
        if isinstance(sample, ControllerTickInput):
            tick = sample
        else:
            # Backward-compatible bridge for the pre-scheduler service. The
            # dedicated scheduler sends ControllerTickInput directly.
            controller_dt = float(self.controller.params.get("dt", sample.dt_s))
            tick = ControllerTickInput(
                tick_seq=self.controller.frame_index + 1,
                controller_dt_s=controller_dt,
                elapsed_s=(self.controller.frame_index + 1) * controller_dt,
                growth_sample=sample,
                growth_sample_age_s=0.0,
                process_state=process_state,
            )
        output = self.controller.step(tick)

        # Do not manufacture an output while the algorithm is warming up.
        if output is None:
            return None

        if isinstance(output, ControllerStepResult):
            return output

        if isinstance(output, Mapping):
            return ControllerStepResult.from_mapping(output)

        raise TypeError(
            "CrystallizationController.step() must return ControllerStepResult, "
            "a result mapping, or None."
        )

    def stop(self) -> None:
        self.controller.stop()

    def add_seed(self, event: Mapping[str, Any]) -> None:
        self.controller.add_seed(event)

    def set_adaptation(
        self,
        enabled: bool,
        mode: str,
        event: Mapping[str, Any] | None = None,
    ) -> None:
        self.controller.set_adaptation(enabled, mode, event)

    def export_state(self) -> Mapping[str, Any] | None:
        return self.controller.export_state()

    def restore_state(
        self,
        params: Mapping[str, Any],
        run_id: str,
        state: Mapping[str, Any],
    ) -> bool:
        return self.controller.restore_state(params, run_id, state)


__all__ = ["CrystallizationControllerAdapter"]
