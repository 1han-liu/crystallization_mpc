# Gsensor Hough Test Memory

## Current Goal

Build MATLAB/Python parity tests for the gsensor Hough sub-pipeline using MATLAB-exported `.mat` fixtures.

The immediate scope is the first three layers:

1. `hough()`
2. MATLAB current rho filtering statement
3. `houghpeaks()`

`houghlines()` is intentionally not included yet.

## Files Added

- `tests/data/gsensor/`
  - Contains four MATLAB debug fixtures:
    - `hough_debug_20260626_120017132_000001.mat`
    - `hough_debug_20260626_120018393_000002.mat`
    - `hough_debug_20260626_120020525_000003.mat`
    - `hough_debug_20260626_120021399_000004.mat`
- `tests/gsensor_hough/`
  - Dedicated folder for Hough-specific tests.
- `tests/gsensor_hough/test_hough_matlab_parity.py`
  - Contains the first three parity tests.
- `gsensor_hough_test_plan.md`
  - Project-level plan describing the testing approach and fixture fields.

## Fixture Format

The four `.mat` files use a top-level `hough_debug` struct.

Relevant fields:

- `I_hough_input`: binary image passed into MATLAB `hough`.
- `theta_input`: theta array passed into MATLAB `hough`.
- `H_raw`: MATLAB `hough` accumulator output.
- `rho_output`: MATLAB `hough` rho output.
- `rho_min_max`: `[rho_min, rho_max]`.
- `H_rho_filtered`: MATLAB current `Hs` after rho filtering.
- `peaks_matlab_1based`: MATLAB `houghpeaks` output, 1-based.
- `lines_hough_raw`: MATLAB `houghlines` output.
- `line_before`
- `params_G`

The current four fixtures are enough for `hough`, rho filtering, and `houghpeaks`.
Only fixtures `000002` and `000004` have non-empty `lines_hough_raw`, so they are the useful cases for future `houghlines()` tests.

## Tests Implemented

### `test_hough_matches_matlab`

Calls current project code:

```python
update_line_module.hough(case.I_hough_input, theta=case.theta_input)
```

Compares against:

- `theta_input`
- `rho_output`
- `H_raw`

Purpose:

Confirm whether Python `hough()` matches MATLAB `hough(I_hough, 'theta', theta_range)`.

### `test_rho_filter_matches_matlab`

Does not call current project code directly, because rho filtering is currently inline inside `update_line()` and is not exposed as a separate function.

The test uses helper `apply_current_matlab_rho_filter()` to reproduce the current MATLAB statement exactly:

```matlab
Hs(rhos < rho_min | rhos > rho_max) = 0;
```

Important detail:

This is MATLAB column-major linear indexing into `Hs(:)`, not row-wise rho filtering.

Purpose:

Confirm that `H_rho_filtered` in the fixtures corresponds to the current MATLAB code as executed.

### `test_houghpeaks_matches_matlab`

Calls current project code:

```python
update_line_module.houghpeaks(case.H_rho_filtered, params_G.num_peak, nhood_size=(9, 1))
```

Compares against:

- `peaks_matlab_1based - 1`

Purpose:

Check whether Python `houghpeaks()` matches MATLAB `houghpeaks()` when given the same filtered H matrix.

## Current Test Result

Command run:

```powershell
.\.venv\Scripts\python -m pytest tests\gsensor_hough -q
```

Current result:

```text
8 failed, 4 passed
```

Passing tests:

- All 4 `test_rho_filter_matches_matlab` cases pass after changing the test helper to reproduce current MATLAB linear indexing semantics.

Failing tests:

- All 4 `test_hough_matches_matlab` cases fail.
- All 4 `test_houghpeaks_matches_matlab` cases fail.

## Known Findings

### Python `hough()` Does Not Match MATLAB Yet

For all four fixtures:

```text
MATLAB rho_output: -1815 to 1815, shape (3631,)
Python rho_output: -1817 to 1817, shape (3635,)
```

This indicates the current Python `hough()` rho range differs from MATLAB.

Earlier manual checks showed MATLAB `H_raw` matches when using:

- 0-based pixel coordinates for accumulator voting.
- `ceil(hypot(height - 1, width - 1))` for rho range.

The current Python implementation uses:

```python
diag = int(np.ceil(np.hypot(height, width)))
x = x.astype(float) + 1.0
y = y.astype(float) + 1.0
```

So the likely fix is in `update_line.py::hough()`.

### Rho Filtering Fixture Is Now Understood

The fixture field `H_rho_filtered` is not the result of row-wise filtering:

```python
Hs[(rhos < rho_min) | (rhos > rho_max), :] = 0
```

It corresponds to current MATLAB code:

```matlab
Hs(rhos < rho_min | rhos > rho_max) = 0;
```

That MATLAB statement uses column-major linear indexing. The test helper now reproduces that behavior, and all rho filter tests pass.

### Python `houghpeaks()` Does Not Match MATLAB Yet

Given the same `H_rho_filtered`, Python `houghpeaks()` still differs from MATLAB.

Observed differences include:

- Different peak order in some cases.
- Python returning more peaks than MATLAB in cases where MATLAB returned fewer.
- Differences likely involve MATLAB `houghpeaks()` default threshold, tie-breaking, and neighborhood suppression behavior.

## Not Done Yet

- Did not modify production code in `src/crystallization_mpc/apps/gsensor/detection/update_line.py`.
- Did not change Python `hough()` implementation yet.
- Did not extract rho filtering into a production helper function.
- Did not modify production rho filtering behavior to match current MATLAB linear indexing.
- Did not implement `houghlines()` parity tests.
- Did not implement full `update_line()` pipeline parity tests.
- Did not decide whether production Python should:
  - strictly match current MATLAB behavior, including likely MATLAB rho-filter indexing bug; or
  - keep/fix row-wise rho filtering as the intended algorithm and regenerate MATLAB fixtures after correcting MATLAB.

## Recommended Next Steps

1. Decide migration policy for rho filtering:
   - strict parity with current MATLAB code, or
   - corrected row-wise rho filtering.
2. Fix `hough()` first if the goal is strict MATLAB parity.
3. Rerun:

   ```powershell
   .\.venv\Scripts\python -m pytest tests\gsensor_hough -q
   ```

4. After `hough()` passes, investigate `houghpeaks()` against MATLAB behavior.
5. Consider extracting production rho filtering into a named function so tests can call project code directly instead of using a test-only helper.
6. Add `houghlines()` tests using fixtures `000002` and `000004`.
