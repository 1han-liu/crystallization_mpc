"""Internal controller-clock input, independent from Gsensor frame delivery."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

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
    runtime_update: Mapping[str, Any] | None = None
    control_hold_reason: str | None = None

    def __post_init__(self) -> None:
        if self.control_hold_reason is not None and (
                not isinstance(self.control_hold_reason, str) or not self.control_hold_reason.strip()):
            raise ValueError("control_hold_reason must be nonempty text or null.")
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
        if self.runtime_update is not None:
            if not isinstance(self.runtime_update, Mapping):
                raise ValueError("runtime_update must be an object.")
            event_id = self.runtime_update.get("event_id")
            if not isinstance(event_id, str) or not event_id.strip():
                raise ValueError("runtime_update requires event_id.")
            if not isinstance(self.runtime_update.get("changes", {}), Mapping):
                raise ValueError("runtime_update changes must be an object.")
            if ("parameter_changes" in self.runtime_update
                    and not isinstance(self.runtime_update["parameter_changes"], Mapping)):
                raise ValueError("runtime_update parameter_changes must be an object.")
            if not self.runtime_update.get("changes") and not self.runtime_update.get("parameter_changes"):
                raise ValueError("runtime_update requires a nonempty patch.")


__all__ = ["ControllerTickInput"]
