"""Capture the existing tick-62 input without altering parameters or source."""
from pathlib import Path
import argparse
import json
from unittest.mock import patch

import numpy as np
from scipy.io import savemat

from crystallization_mpc.apps.controller.algorithm import controller as module
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from run_controller_simulation import fixed_inputs, make_controller, no_network


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    original = module.adapt_growth_parameters
    captured = {}
    c = make_controller({"run_type": "simulation", "growth_rate_source": "simulated",
                         "simulation_noise": fixed_inputs(80)}, "reference-input", "all")

    def capture(params, growth, sigma, temperature, selected, maximum, minimum, mode):
        if c.frame_index == 62:
            captured.update(params=params.copy(), growth=np.array(growth), sigma=np.array(sigma),
                            temperature=np.array(temperature), selected=np.array(selected),
                            maximum=maximum, minimum=minimum,
                            raw_growth=np.array(c.history["G_measure"]))
        return original(params, growth, sigma, temperature, selected, maximum, minimum, mode)

    with no_network(), patch.object(module, "adapt_growth_parameters", capture):
        for i in range(1, 63):
            if i == 11:
                c.add_seed({"event_id": "reference-seed"})
            c.step(ControllerTickInput(i, 5., i * 5.))
    # Only scalar numeric parameters are needed by the MATLAB function.
    captured["params"] = {k: v for k, v in captured["params"].items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool)}
    savemat(args.output / "inputs.mat", captured, oned_as="row")
    (args.output / "provenance.json").write_text(json.dumps({
        "tick": 62, "seed_tick": 11, "initial_concentration": .32,
        "source": "Python direct controller simulation, captured before first fit at tick 62",
        "purpose": "Identical-input MATLAB function replay; not MATLAB closed-loop equivalence"
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
