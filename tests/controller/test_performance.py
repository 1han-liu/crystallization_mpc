from __future__ import annotations

import time

import numpy as np

from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.apps.controller.algorithm.controller import CrystallizationController


def test_algorithm_tick_performance_on_frozen_environment() -> None:
    controller = CrystallizationController()
    controller.configure(
        {"run_type": "simulation", "growth_rate_source": "simulated"},
        "performance",
    )
    controller.start()
    durations: list[float] = []
    for index in range(1, 1021):
        started = time.perf_counter()
        result = controller.step(ControllerTickInput(index, 5.0, index * 5.0))
        duration = time.perf_counter() - started
        assert result is None or result.valid
        if index > 20:
            durations.append(duration)
    assert len(durations) == 1000
    assert float(np.percentile(durations, 95)) < 4.0
    assert max(durations) < 5.0
