# MATLAB R2021a golden-data harness

Run this finite, non-OPC harness with the frozen MATLAB reference worktree:

```bash
/home/laniakea/.local/MATLAB/R2021a/bin/matlab -batch \
  "addpath('tests/controller/matlab_harness'); generate_golden_fixtures"
```

The harness never runs `controller_for_gui.m`, opens the GUI, or writes to a
device. It calls only pure numerical functions and finite EKF/adaptation
sequences. Random values needed by Python are materialized in the MAT fixture;
Python never assumes NumPy reproduces MATLAB's RNG.

## Adaptation failure/edge-case probes

Use the same user-local **R2021a Update 8** runtime above, not the separate
`/usr/local/MATLAB/R2021a` installation (missing toolbox implementations).
The reference worktree must remain at `ce885a13e0a3e95ac0509e06eebf9d7cd1d418b0`.

```bash
.venv/bin/python -B scripts/capture_adaptation_reference_inputs.py --output .runtime/new-adaptation-inputs
/home/laniakea/.local/MATLAB/R2021a/bin/matlab -batch "addpath('tests/controller/matlab_harness'); probe_adaptation_reference('.runtime/new-adaptation-inputs/inputs.mat','.runtime/new-adaptation-inputs/oracle.mat'); probe_adaptation_partial_state('.runtime/new-adaptation-inputs/inputs.mat','.runtime/new-adaptation-inputs/partial-state.mat')"
```

The capture uses the original default initial conditions and tick-11 seed event.
It captures histories at tick 62 *before* the first fitting call. The probes run
only the frozen numerical function and adaptation/recording snippets; never the
GUI, broker or OPC UA loop. All output paths must be new to preserve evidence.
These fixtures establish same-input function and partial-state behavior, not a
full closed-loop MATLAB simulation. Current numerical discrepancies are listed
in `docs/controller-reference-validation.md` and deliberately fail the tests.
