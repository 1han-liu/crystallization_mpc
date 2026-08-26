"""Internal controller-clock input, independent from Gsensor frame delivery."""

from __future__ import annotations

import math
from dataclasses import dataclass

from crystallization_mpc.apps.controller.process import ProcessState
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload


@dataclass(frozen=True)
class ControllerTickInput:
    tick_seq: int
    controller_dt_s: float
    elapsed_s: float
    growth_sample: GrowthRateSamplePayload | None = None
    growth_sample_age_s: float | None = None
    process_state: ProcessState | None = None

    def __post_init__(self) -> None:
        if isinstance(self.tick_seq, bool) or int(self.tick_seq) < 1:
            raise ValueError("tick_seq must start at 1.")
        for name in ("controller_dt_s", "elapsed_s"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if self.growth_sample_age_s is not None:
            age = float(self.growth_sample_age_s)
            if not math.isfinite(age) or age < 0:
                raise ValueError("growth_sample_age_s must be finite and nonnegative.")
        if self.growth_sample is None and self.growth_sample_age_s is not None:
            raise ValueError("A growth sample age requires a growth sample.")


__all__ = ["ControllerTickInput"]
