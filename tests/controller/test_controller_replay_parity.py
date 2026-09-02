from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.io import loadmat

from crystallization_mpc.apps.controller.process import ProcessState
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.apps.controller.algorithm.controller import CrystallizationController
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload


GOLDEN = loadmat(
    Path(__file__).parent / "fixtures/matlab_r2021a_golden.mat",
    simplify_cells=True,
)


def _sample(frame: int) -> GrowthRateSamplePayload:
    return GrowthRateSamplePayload(
        run_id="replay",
        frame_seq=frame,
        image_name=f"replay-{frame}.png",
        captured_at="2026-08-26T12:00:00Z",
        processed_at="2026-08-26T12:00:01Z",
        dt_s=15.0,
        valid=True,
        status="measuring",
        G_u=3e-8,
        G_u_KF=3e-8,
        G_v=3e-8,
        G_v_KF=3e-8,
    )


@pytest.mark.parametrize("case_index", range(4))
def test_finite_experiment_replay_matches_matlab_r2021a(case_index: int) -> None:
    case = GOLDEN["replay"][case_index]
    controller = CrystallizationController()
    controller.configure(
        {
            "mode": case["mode"],
            "target": case["target"],
            "run_type": "experiment",
            "growth_rate_source": "live_gsensor",
        },
        "replay",
    )
    controller.start()
    outputs = []
    for index in range(8):
        process = ProcessState(
            T=float(case["T"][index]),
            T_j=float(case["T_j"][index]),
            c=float(case["c"][index]),
            count_middle=100.0,
            T_j_set=315.15,
            read_at="2026-08-26T12:00:00Z",
        )
        outputs.append(
            controller.step(
                ControllerTickInput(
                    tick_seq=index + 1,
                    controller_dt_s=5.0,
                    elapsed_s=(index + 1) * 5.0,
                    growth_sample=_sample(index + 1),
                    growth_sample_age_s=0.0,
                    process_state=process,
                )
            )
        )
    assert all(output is not None and output.valid for output in outputs)
    states = np.array(
        [[output.T_KF, output.dT_dt_KF, output.c_KF, output.dc_dt_KF] for output in outputs],
        dtype=float,
    ).T
    np.testing.assert_allclose(states, case["states"], rtol=1e-6, atol=2e-8)
    np.testing.assert_allclose(
        [output.sigma for output in outputs], case["sigma"], rtol=1e-6, atol=1e-10
    )
    np.testing.assert_allclose(
        [output.G_model for output in outputs], case["G"], rtol=1e-6, atol=1e-12
    )
    np.testing.assert_allclose(
        [output.dT_dt_set for output in outputs], case["dT_dt_set"], rtol=1e-5, atol=1e-9
    )
    np.testing.assert_allclose(
        [output.T_j_set for output in outputs], case["T_j_set"], rtol=1e-8, atol=1e-5
    )
    np.testing.assert_allclose(
        [output.objective for output in outputs], case["objective"], rtol=1e-5, atol=1e-10
    )
