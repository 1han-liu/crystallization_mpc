"""Internal controller-clock input, independent from Gsensor frame delivery."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from crystallization_mpc.apps.controller.process import ProcessState
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload


class ControllerInputValueError(ValueError):
    """A numeric input failed validation at an actual MATLAB calculation boundary."""

    def __init__(self, message: str, *, stage: str = "main_filter",
                 reference_identifier: str | None = None) -> None:
        self.stage = stage
        self.reference_identifier = reference_identifier
        super().__init__(message)


class ControllerGrowthMeasurementError(ControllerInputValueError):
    """A successful G read produced a non-finite MATLAB correct() input."""

    def __init__(self, message: str) -> None:
        super().__init__(message, stage="growth_filter",
                         reference_identifier="MATLAB:ExtendedKalmanFilter:expectedFinite")


@dataclass(frozen=True)
class ControllerTickInput:
    tick_seq: int
    controller_dt_s: float
    elapsed_s: float
    growth_sample: GrowthRateSamplePayload | Mapping[str, Any] | None = None
    growth_sample_age_s: float | None = None
    process_state: ProcessState | None = None
    runtime_update: Mapping[str, Any] | None = None
    control_hold_reason: str | None = None
    # A local call boundary, deliberately absent from serialized commands and
    # checkpoints. MATLAB reads G after computing this cycle's control request.
    process_reader: Callable[[str], Any] | None = None
    growth_active: bool | None = None
    growth_reader: Callable[[], GrowthRateSamplePayload | Mapping[str, Any] | None] | None = None

    def __post_init__(self) -> None:
        # A source dt/value is evaluated at its actual MATLAB statement. This
        # input container must not introduce an earlier numerical rejection.
        pass


__all__ = ["ControllerInputValueError", "ControllerGrowthMeasurementError", "ControllerTickInput"]
