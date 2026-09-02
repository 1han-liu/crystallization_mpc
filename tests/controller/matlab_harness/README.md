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
