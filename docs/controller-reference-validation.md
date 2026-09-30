# Controller reference validation

> Distribution note: the test suite, MATLAB fixtures/harness, and numerical
> audit scripts described here are local-only and excluded from Git. References
> below preserve validation provenance; reproduction requires those local
> development files and is not available from a fresh clone alone.

## September 17 consistency work — offline acceptance

The current work uses `supersaturation_control.m` as the principal closed-loop
oracle and retains the GUI-snippet comparisons. A stable sigma plot is not proof
of equivalence. The original 13 adaptation numerical failures are now resolved
against unchanged fixtures: the final Controller plus Central runtime/history
suite has 469 passes, and
140 new original-MATLAB adaptation inputs also pass rtol=1e-5, atol=0.
The 14 principal long-loop scenarios, staged GUI sequence, exact JSON recovery,
cross-mode same-input tests and thermal boundary oracle also pass. Historical
failures below describe earlier snapshots, not the final candidate.
Additional September 17 result reports and manual rerun notes are retained
locally and are not distributed with this repository. This is offline
numerical/integration acceptance, not equipment validation or deployment.

Simulation and Experiment share the numerical controller; only process input
and actuation differ. The original MATLAB activation scalar and its dynamic
history have different roles:

| Quantity | Lifetime and use |
|---|---|
| `T_j` | Current process jacket temperature; continues changing in the plant and telemetry |
| `T_j_set` | Newly calculated jacket command; continues changing |
| `control_T_j_reference_K` | Activation scalar passed to `calc_T_j_set` in both modes; not a frozen physical plant |

Simulation initializes the reference from `T_j_init`. Experiment reads and
validates an activation `ProcessState` before starting the adapter, initializes
filters and reference through `initialize_process_state`, and only then enters
RUNNING. The first scheduled tick performs a separate read. Neither target
switching nor adaptation switching relatches the reference. Initialization
failure prohibits operation and actuator output. The hook defaults to a no-op
for third-party adapters; the crystallization algorithm requires it in Experiment.

The single algorithm checkpoint format persists this reference, filters, and the
fitting-only raw growth history, without a numeric algorithm format label.
Recovery checks actual content, run identity, baseline, and parameter digest;
it must not infer a missing reference from a new measurement. A legacy numeric
label is ignored when all required content is valid. Incomplete or corrupt
checkpoints are rejected, not migrated or deleted. On recovery failure, automatic
saving (including shutdown) is inhibited to preserve the original file; start a
new experiment using a new session directory. The outer service schema and
parameter revisions are unchanged. Older code may not read the new unnumbered
algorithm format; backwards restart compatibility is not promised. This code is
not hot-deployed into an existing experiment.

The raw growth history used by the fitting EKF is separate from continuous
dashboard `G_measure`: only adaptation execution assigns entries, and MATLAB
indexed-assignment gaps are zero-filled. Disabling adaptation does not feed
dashboard observations into the fitting filter. The history survives recovery.

The principal 14-case long-loop replay exposed a further difference, invisible
in the original short helper tests: with G adaptation off and shared noise,
the state crosses the `abs(sigma) < 0.001` dynamics branch around tick 327.
SciPy's unrestricted default RK45 maximum step did not match MATLAB's default
`MaxStep = abs(tf-t0)/10`. On identical inputs this changed the predicted state
and covariance, then the controller's hold branch at tick 328. Both Python
transition functions now explicitly use MATLAB's interval-based maximum step;
neither control dt, formulas, gains nor numerical acceptance tolerances changed.
The default is documented in [MathWorks odeset](https://www.mathworks.com/help/matlab/ref/odeset.html)
and verified in the installed R2021a runtime. The 26 same-input boundary probes
now have identical transition vectors. All 14 closed-loop scenarios were then
rerun and audited against the current production source hashes.

Physical device validation remains NOT RUN. In particular, the meaning and
availability of the configured `T_j_node` must be confirmed before equipment
use. No offline process fake establishes that the physical value is measurable.

## Frozen baseline

The only numerical oracle for this validation is
`HuitianYu/MPCrystal` `main@ce885a13e0a3e95ac0509e06eebf9d7cd1d418b0`.
It is inspected through the detached, clean worktree
`/home/laniakea/Desktop/iiot-enabled-sensorized-control-platform-for-seeded-crystallization/MPCrystal_original_matlab`.
The original and locally merged `integration` branches, stashes, and uncommitted
Gsensor work are deliberately excluded.

Golden data is generated by MATLAB R2021a Update 8. The reference installation
provides `extendedKalmanFilter`, `fmincon`, and `ode45`; it does not provide
`opcua`, so fixtures never run the GUI/OPC loop.

The Python implementation is based on
`crystallization_mpc/main@f903e4a5dfc07d22ed27db7a755456b6cc5d8b1c`
on `feature/controller-matlab-parity`. Exact Python numerical dependencies are
recorded in `tests/controller/constraints.txt`.

## Responsibility and interface boundary

The Controller algorithm owns the numerical behavior and persistent state of
`controller_for_gui.m`, `subroutines_controller`, and the calculation portions
of `gui_main/snipptets_controller`. It also owns simulation process-state
propagation and simulated growth-rate generation.

The platform owns Central/UI, Gsensor image processing and publication,
RabbitMQ, OPC UA reads/writes, InfluxDB, Docker, experiment directories, the
independent controller scheduler/G cache, and the read/write shadow gate. Shared
interfaces are changed only through an explicit contract. Platform transports
SI values to the controller; the algorithm never imports transport, database,
UI, or OPC code.

The v1 tick contract is `ControllerTickInput`: `tick_seq`,
`controller_dt_s`, `elapsed_s`, optional latest growth sample plus its age, and
optional `ProcessState`. Experiment ticks require process state. Simulation may
own its state and accept `ProcessState=None`. The controller clock is `dt=5 s`;
the independent Gsensor clock is `dt_G=15 s`. A cached G sample does not advance
algorithm time. Adaptation pauses when the sample is absent or older than
`2*dt_G`, while ordinary control may continue.

Units are fixed: `T`, `T_j`, and `T_j_set` are kelvin; growth rates are metres
per second; `c` preserves MATLAB's scalar g/gH2O convention; `count_middle`
preserves the device scalar convention; time is seconds.

## Traceability matrix

Status legend: **convert** is production numerical Python; **platform** is
replaced by an existing platform boundary; **non-production** is GUI/plot/save
behavior; **canonical duplicate** maps byte-identical MATLAB copies to one
Python implementation; **backup** is excluded `.asv` material.

| MATLAB source | Status | Python destination / verification |
|---|---|---|
| `source_codes/supersaturation_control.m` | principal numerical oracle | finite extraction in local `main_closed_loop_run.m`; original main-script initialization, timed seed and numerical loop, with all transformations recorded |
| `source_codes/controller_for_gui.m` | convert + platform | `algorithm/controller.py`; lifecycle and finite replay tests; communication/GUI parts are platform-owned |
| `source_codes/parameters.m` | contract | `baseline_manifest.json`, Central parameter contract tests |
| `source_codes/parameters_on_target_change.m` | convert | target-dependent parameter selection in `algorithm/parameters.py` |
| `source_codes/parameters_G.m` | contract | timing/G-source entries in manifest |
| `source_codes/gui_main/op_section.m` | contract + platform | operation-mode enums and declared UI metadata are tracked in the manifest; OperationsTab list values come from the base workspace; UI actions remain platform-owned |
| `source_codes/calc_mode.m` | convert exactly | compatibility helper in `algorithm/dynamics.py`; deviation D-001 |
| `control/adapt_growth_parameters.m` | convert | `algorithm/adaptation.py`; seven-mode oracle tests |
| `control/add_to_int_X_dt.m` | convert | `algorithm/control.py`; algebra oracle |
| `control/assign_list.m` | convert | direct tuple assignment; trace-only test |
| `control/calc_G.m`, `calc_c_meta.m`, `calc_c_sat.m`, `calc_dc_sat_dT.m`, `calc_relative_sigma.m`, `calc_sigma.m` | convert | `algorithm/thermodynamics.py`; algebra oracle |
| `control/calc_K_I_target.m`, `calc_K_P_target.m` | convert | `algorithm/parameters.py`; mode oracle |
| `control/calc_Q.m`, `calc_R.m`, `measurement_function.m`, `measurement_matrices.m` | convert | `algorithm/ekf.py`; matrix oracle |
| `control/state_transition_matrices.m`, `state_transition_matrices_model.m` | convert | `algorithm/dynamics.py`; matrix oracle |
| `control/construct_EKF.m`, `create_EKF_general.m`, `smooth_EKF.m`, `smooth_EKF_general.m` | convert | explicit state/covariance EKF in `algorithm/ekf.py`; trajectory oracle |
| `control/calc_T_j.m`, `calc_T_j_set.m`, `calc_T_j_set_.m`, `calc_T_with_zero_dT_dt.m`, `calc_dT_dt_model.m`, `calc_dT_dt_set.m`, `calc_dT_j_dt.m`, `calc_e_dT_dt.m`, `calc_e_target.m`, `objective_function.m`, `update_T_j.m` | convert | `algorithm/control.py`; PI/MPC and trajectory oracles |
| `control/calc_dT_dt.m`, `calc_dc_dt.m`, `calc_t_lag_perc.m`, `calc_t_lag_perc2.m`, `clip.m` | convert | `algorithm/control.py`; boundary tests |
| `control/state_transition_ODE.m`, `state_transition_ODE_T.m`, `state_transition_function.m`, `state_transition_function_T.m` | convert | `algorithm/dynamics.py`; RK45 oracle |
| `control/calc_size_from_volume.m`, `calc_surface_area.m`, `calc_volume.m` | canonical duplicate | `algorithm/mass_balance.py`; identical to mass-balance copies |
| `control/read_value.m`, `safe_read_value.m` | platform | `apps/controller/process.py`; not called by numerical code |
| `mass_balance/calc_next_crystallization_mass_balance.m`, `create_size_list.m` | convert | `algorithm/mass_balance.py`; deterministic fixture arrays |
| `mass_balance/calc_size_from_volume.m`, `calc_surface_area.m`, `calc_volume.m` | convert/canonical | `algorithm/mass_balance.py`; algebra oracle |
| `utils/fill_target_list.m`, `initialize_lists.m` | convert | controller state/history helpers and mode tests |
| `utils/initialize_G.m` | split | simulated branch in the algorithm controller; image/Gsensor initialization is platform-owned |
| `utils/create_nodes.m` | platform | `apps/controller/process.py` |
| `utils/add_point.m`, `add_points.m`, `create_animated_line.m`, `initialize_animated_lines.m`, `initialize_plots.m`, `plot_line.m`, `plot_lines.m`, `replot_results.m` | non-production | Central UI/telemetry; not imported by algorithm |
| `utils/add_points.asv`, `initialize_plots.asv` | backup | excluded and recorded only |
| `snipptets_controller/calculate_target.m`, `calculate_lag_time.m`, `calculate_T_j_set.m`, `control_target.m` | convert | ordered operations in `CrystallizationController.step`; replay oracle |
| `snipptets_controller/execute_growth_parameters_adaption.m`, `record_growth_parameters.m`, `refresh_adaptive.m`, `refresh_controller.m` | convert | adaptation/EKF state in algorithm modules; state tests |
| `snipptets_controller/initialize_controller.m`, `initialize_controller_for_gui.m`, `record_controller_time.m` | convert | `CrystallizationController.start/step`; lifecycle tests |
| `snipptets_controller/measure_controller_data.m` | split | experiment values come from tick input; simulation transition/noise is converted |
| `snipptets_controller/read_growth_rate.m` | split | simulated model/noise is converted; RabbitMQ read branch is platform-owned |
| `snipptets_controller/initialize_inline_display.m`, `save_inline_display.m`, `pop_up_and_save_variables.m` | non-production | Central UI, telemetry and platform persistence |

All 63 production `.m` files and both `.asv` files under
`subroutines_controller`, all 16 controller snippets, the five related
top-level parameter/mode/loop sources, and `gui_main/op_section.m` are
represented above: 87 classified sources in total. The same inventory is
machine-readable in `baseline_manifest.json` and is tested directly against the
frozen reference worktree. Grouped rows list every source basename explicitly.

## Parameter contract

The machine-readable contract is
`tests/controller/fixtures/baseline_manifest.json`. It contains source and
runtime names, types, units, defaults, target-dependent selections, derivation
formulas, supported enum values, ownership, solver choices, fixture hashes, and
numeric tolerances. Central's checked-in defaults must agree with the source
defaults before integration.

The four confirmed pre-conversion drifts are:

| key | MATLAB baseline | original Python value | disposition |
|---|---:|---:|---|
| `sigma_set` | `0.12` | `0.035` | align to MATLAB baseline |
| `min_num_adapt` | `30` | `15` | align to MATLAB baseline |
| `max_num_adapt` | `1000` | `50` | align to MATLAB baseline |
| `T_init_G` | `311.65 K` | `315.15 K` | align to MATLAB baseline |

## Deviation ledger

| ID | Reference behavior | v1 decision | Guard |
|---|---|---|---|
| D-001 | `calc_mode()` always returns `MPC`, although UI/config exposes PI | reproduce exactly; explicit `mode='PI'` branches still use PI | mixed-semantics PI oracle fixture |
| D-002 | `calc_T_j_set_.m` assigns local `K_P_T` from `params.K_P_target`, not `params.K_P_T` | reproduce exactly, do not silently correct | direct function oracle and code comment |
| D-003 | MATLAB and NumPy RNG streams differ for the same seed | store MATLAB-generated random arrays in fixtures and replay them in Python | fixture hashes and deterministic repeat test |
| D-004 | `op_section.m` declares `exp_sim='experiment'` and `adaptive_mode='E_A'`, while `parameters.m` initializes `simulation` and `all`; `OperationsTab` actually calls `evalin('base', ...)` for list values | preserve `parameters.m` algorithm defaults; Central sends an explicit safe run configuration (`experiment/MPC/sigma/E_A/live_gsensor`) | machine-readable operation contract and Central default test |
| D-005 | MATLAB activation scalar `T_j` and dynamic `T_j_list(ii)` are distinct | one `control_T_j_reference_K` lifetime in both Python modes; dynamic process state continues updating | startup snapshot, restore and exact same-input cross-mode tests |
| D-006 | MATLAB ode45 default maximum internal step is one tenth of the integration interval | explicit `max_step=abs(dt)/10` in both Python transitions; do not retune dt or tolerances | same-input piecewise-boundary probe and full long-loop replay |
| D-007 | Main script catches a fitting exception locally; GUI outer catch skips later recording/output | preserve GUI-oriented platform failure protection; document the entrypoint difference rather than treating an invalid tick as a successful fit | original exception fixtures; invalid results never cause equipment writes; normal-path main and GUI oracles both retained |
| D-008 | Main script seeds by simulated time; Central is operator-controlled | map fixed MATLAB seed times to explicit offline events, do not add automatic UI seeding | seed tick assertions; manual tests record actual user event times |

Intentional fixes require separate approval, a separate commit, and both
baseline-compatible and corrected-behavior tests.

## Return and safety semantics

- `None` means normal warm-up or insufficient data and never causes a write.
- `ControllerStepResult(valid=False, error=...)` means an expected numerical or
  temporary-input failure and never causes a write.
- Exceptions mean lifecycle/state/interface/programming failure. The service
  enters `ERROR` and disconnects the device.
- A valid result always has a finite, constrained real `T_j_set`.
- MPC rejects infeasible/non-finite bounds before invoking SciPy and aborts
  between objective evaluations after a 4 s monotonic deadline. Infeasible,
  timed-out, and unsuccessful optimizations all produce invalid results and no
  device write; normal solver inputs and MATLAB parity tolerances are unchanged.
- `CONTROLLER_OPCUA_WRITE_ENABLED=false` is a separate, default-deny write gate.
  Shadow mode may read live state but its write-call count must remain zero.

### R2021a adaptation strategy audit — 2026-09-09 (NOT fully accepted)

The requested change preserves the frozen MATLAB adaptation strategy, rather
than introducing positive-sigma sample filtering or continuing a failed cycle
as a valid controller output. Baseline/default parameters were not edited.

- Keep the original selection order: `to_adapt`, non-NaN G, positive G,
  most-recent window, `abs(sigma)<1e-15` replacement, minimum count 30.
- Evaluate the original `log(k0 * sigma.^n * exp(-EA/R/T))` objective, including
  complex arithmetic. The former `n*log(sigma)` form and blanket sigma/T domain
  rejection did not reproduce all reference inputs (e.g. negative sigma with
  fixed even integer n). Initial objective/finite-difference errors now carry
  the observed MATLAB identifiers. These are compatibility diagnostics; Python
  does not actually invoke fmincon.
- Failed adaptation leaves caller params/count unchanged, but preserves the
  already updated histories, controller/growth EKFs, integrals and simulated
  setpoint. It logs a warning, skips subsequent parameter recording, returns an
  invalid result (no device write), and permits the next tick. When recording
  resumes, MATLAB's indexed-assignment gaps in kinetic histories are zero-filled.
- Historically, partial parameter histories introduced schema **2**, and the
  September 17 initialization patch introduced schema **3**. The later single
  algorithm format described above supersedes those numeric checks. A legacy
  label alone neither accepts nor rejects an archive: all required current
  content must be valid. Incomplete archives are preserved, not silently
  resumed or repaired. Other optimization/input safety handling is unchanged.

Actual reference runtime: `/home/laniakea/.local/MATLAB/R2021a/bin/matlab`,
R2021a Update 8. The separate `/usr/local/MATLAB/R2021a` installation lacks the
required toolbox implementations and was not used to produce the oracles.

New frozen fixtures:

- `tests/controller/fixtures/matlab_r2021a_adaptation_edges.mat`: 70 direct
  calls of the unchanged MATLAB function, 10 cases × seven modes. Includes
  the same Python tick-62 input, 29/30 gates, negative sigma with integer n,
  tiny negative/zero sigma, NaN sigma/T, zero T and recent-window truncation.
- `tests/controller/fixtures/matlab_r2021a_adaptation_partial_state.mat`:
  original MATLAB adaptation/recording snippets with supplied histories;
  verifies partial state and zero-filled gaps across failures. This is NOT a
  full MATLAB closed-loop trajectory replay.

Results: **70/70 outcome/count/error-category matches**. Of 38 successful MATLAB
function cases, **25 meet the existing rtol=1e-5 parameter gate; 13 fail it**.
The existing nine numeric-parity tests still pass. The full controller suite
has **201 passed, 13 failed**. The failing tests are retained as real failures,
not skipped/xfail; tolerances were not relaxed.

A read-only comparison with the pre-change HEAD implementation found that 12
of these numerical failures already existed on the same newly added inputs.
Across 35 previously accepted cases, old/new Python parameter differences were
at most 1.28e-16 relative. The remaining failed case is a fixed-integer-n input
that the former blanket sigma rejection did not allow to fit at all.

Remaining acceptance blocker: SciPy bounded minimization is not MATLAB
`fmincon`'s interior-point optimizer. It has different iterations and stopping
behavior. Initial-domain checks and ignoring a finite iterate's success flag
do not make the solvers identical. No fixture-specific corrections were added.
Exact fmincon-backed runtime execution would introduce a MATLAB dependency and
requires a separate deployment decision. Full shared-input/noise/seed MATLAB
closed-loop acceptance is still **NOT RUN**, pending this solver decision.

The repeated Python-only 22-scenario audit remains 15 PASS / 7 FAIL. All seven
default adaptation cases still fail starting at tick 62; replaying the identical
tick-62 input in MATLAB also fails with `optim:barrier:UsrObjUndefAtX0`.
Do not confuse successful failure reproduction with successful parameter fitting.

Reproduce the Python checks from the project root:

```bash
.venv/bin/python -B -m pytest tests/controller -q -p no:cacheprovider
.venv/bin/python -B scripts/report_adaptation_reference.py --output .runtime/new-comparison.json
.venv/bin/python -B scripts/run_controller_simulation.py --output .runtime/new-simulation
```

All three commands intentionally exit nonzero while the respective failed gates
remain. Simulation outputs include source hashes; previous reports were not
overwritten. No real experiment, service deployment, commit or push was performed.

## Verification gates

Algebra uses `rtol=1e-10, atol=1e-12`; ODE/EKF uses `rtol=1e-6` plus
quantity-specific absolute tolerances; G error is at most `1e-12 m/s`;
temperature error at most `1e-5 K`; concentration error at most `1e-10` and
its rate at most `1e-12/s`; optimizer bounds may be violated by at most `1e-9`
and objective `rtol=1e-5`; adapted parameters use `rtol=1e-5`.

Validation proceeds through function oracle tests, multi-step replay,
simulation/adapter integration, recovery/fail-closed tests, dual-clock tests,
and finally read-only shadow. Performance is measured after 20 warm-up ticks
over at least 1000 algorithm-only ticks; p95 must be below 4 s and no tick may
reach 5 s on the target machine.

## Implemented verification inventory

- Machine-readable baseline and parameter source contract:
  `tests/controller/fixtures/baseline_manifest.json`.
- MATLAB R2021a golden MAT fixture: algebra, matrices, ODE, controller/general
  EKF trajectories, MPC/PI by sigma/G, eight-tick replay, simulation mass
  balance with seeding, seven adaptation modes, MATLAB RNG arrays, and the
  MATLAB seed population.
- Python tests: parameter and deviation contract, function parity, multi-step
  Controller parity, simulation without process state, seed lifecycle,
  adaptation freshness, JSON recovery and incompatibility rejection,
  return/failure semantics, 3:1 dual-clock behavior, NoOp safety, OPC read-only
  shadow, explicit write gating, and 20+1000 tick performance.

The algorithm adapter remains opt-in. The full verification command is
`python -m pytest tests/controller`; the golden-data regeneration command is
documented in `tests/controller/matlab_harness/README.md`.

On the target development machine on 2026-08-26, the required 20-tick warm-up
plus 1000 measured simulation ticks produced p95 `0.003448 s`, maximum
`0.015908 s`, and mean `0.003192 s` for algorithm computation only.

The on-site shadow evidence collector is
`crystallization_mpc.apps.controller.shadow_audit`. It observes only the
Controller status API and refuses a window unless the algorithm adapter is
running, real OPC UA reads are active, the write gate is disabled, all ticks are
consecutive, every candidate remains inside the configured jacket-temperature
range, and OPC UA write attempts/calls remain zero. Each JSON record contains
the tick, G frame and age, live process snapshot, candidate `T_j_set`, objective,
constraint result, solver validity, and failure reason. The generated evidence
contains real process values and is deliberately excluded from Git.
