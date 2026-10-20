# Prosys integration acceptance

This optional Ubuntu/Podman profile exercises a **real Prosys OPC UA Simulation
Server** through the production message-based process I/O path:

```text
Independent process model -> Prosys -> Telegraf -> RabbitMQ -> Controller
                            ^                                  |
                            +--------- OPC UA Writer <- RabbitMQ
```

The process model reads `T_j_set` back from Prosys and writes four process
variables. A separate **Synthetic G** service supplies model-generated growth
measurements. InfluxDB stores the observations; Grafana reads them for display.
This is neither the accelerated in-process simulation nor a replacement Python
OPC UA server. It does **not** qualify real equipment, cameras, image alignment,
FBRM, nucleation or production GSensor image-parameter changes.

## 1. What a clone provides

- `src/crystallization_mpc/testing/prosys/`: process model, Synthetic G,
  deployment, commissioning, fault checks, evidence readers and stage gates.
- Thirteen reviewed host tools in `integration_tests/prosys/`: deployment
  collection, qualification, short checks, native-fixture checks and bounded
  supervision. These are a complete dependency set, not interchangeable scripts.
- This procedure; it contains no inherited machine qualification or result.

Prosys binaries, saved `.uasim` projects, `.venv`, private configuration, native
process identities, database volumes, reports, screenshots and CSVs are **not**
distributed. Local regression cases and exploratory/candidate algorithms also
remain excluded. Do not run `pytest integration_tests/prosys` expecting a test
suite in a fresh clone: the versioned files there are operational host tools.
The host tools require a repository checkout; installing only a wheel does not
install them or supply the repository configuration used by deployment.

## 2. Safety and machine prerequisites

Use a trusted, isolated test machine/network, never physical equipment or a
production GSensor queue. The profile is named `mpcrystal-prosys`; do not share
its volumes, ports or RabbitMQ consumers with another run. Its HTTP write APIs
are not authenticated and the UA fixture uses anonymous, unencrypted access.
Published container ports bind to `127.0.0.1`, but the **native Prosys listener
may bind all interfaces**. Restrict its listener/network exposure separately;
localhost container bindings do not establish complete network isolation.

Install Python >=3.10, rootless Podman and the official native Linux Prosys
desktop application. Obtain Prosys from its
[official product page](https://prosysopc.com/products/opc-ua-simulation-server/)
and complete the vendor's installation/license prompts. Keep the desktop server
open. The tools do not install Prosys, change the host firewall, enable user
linger or assume a paid headless license. Detached supervision additionally
requires a working user systemd manager and the binaries checked by
`managed_unit.py` (including `systemd-run` and `systemd-inhibit`).

From the **actual clone root**, create a separate environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . podman-compose
export MPC_REPO="$(pwd -P)"
export MPC_PY="$MPC_REPO/.venv/bin/python"
```

Use that interpreter for host commands. Editable installation is important:
detached units must import the package without relying on a shell-only
`PYTHONPATH`. Host Podman installation remains a separate prerequisite.
The commands set `MPC_REPO` and `MPC_PY` to the clone root and its
`.venv/bin/python` **absolute paths**. Do not copy another machine's environment.

Reading CLI `--help` is safe; the subsequent deployment/experiment commands
are **not read-only examples**. Execute each only after deliberate review.
Every output path marked `NEW` must be new; change all downstream references
to the actual local result. Preserve failed attempts rather than overwriting
them. Never copy a JSON PASS label, container identity or old report to obtain
qualification on a new machine.

## 3. Configure the five Prosys nodes

Create scalar **Double**, Constant/external-write nodes, all with Good quality,
client read/write permission, and no inherited or automatic changing value.
Save the project and verify its persistence after reopening it.

| Field | String identifier after `ns=N;s=` | Static value | Unit |
| --- | --- | ---: | --- |
| `T` | `Local.iControl.Experiment1.Trends.Tr.Value` | 25 | degrees C |
| `T_j` | `Local.iControl.Experiment1.Trends.Tj.Value` | 23 | degrees C |
| `c` | `Local.iControl.Experiment1.Trends.load.Value` | 0.32 | g/g water |
| `count_middle` | `Local.iControl.Experiment1.Trends.count.Value` | 17 | count |
| `T_j_set` | `iCache.Temperature1` | 22 | degrees C |

Use the server's **actual** namespace index for `N`; do not assume it is 3.
The examples use namespace 3 and port 53530. Basic commissioning can take an
explicit local endpoint, but the guarded full native-fault profile requires
port **53530**. Do not substitute a real equipment address.
Controller-side process temperatures are Kelvin, so 25 degrees C must arrive
as 298.15 K. The generator owns the four process nodes during dynamic runs;
only the production Writer writes `T_j_set` then.

## 4. Prepare the isolated deployment

After confirming the endpoint and namespace, set the host connection details:

```bash
export PROSYS_TEST_ENDPOINT='opc.tcp://127.0.0.1:53530/OPCUA/SimulationServer'
export OPCUA_NAMESPACE_INDEX=3
export RABBIT_URL='amqp://guest:guest@127.0.0.1:5674/%2F'
export PROCESS_DEVICE_ID='prosys-test'
export PROSYS_TEST_CONTROLLER_URL='http://127.0.0.1:18002'
export PROSYS_TEST_PLANT_URL='http://127.0.0.1:18010'

"$MPC_PY" -B -m crystallization_mpc.testing.prosys.deployment prepare \
  --endpoint "$PROSYS_TEST_ENDPOINT" --namespace "$OPCUA_NAMESPACE_INDEX"
"$MPC_PY" -B -m crystallization_mpc.testing.prosys.deployment build
"$MPC_PY" -B -m crystallization_mpc.testing.prosys.deployment up
"$MPC_PY" -B -m crystallization_mpc.testing.prosys.deployment status
```

`prepare` refuses to overwrite an existing deployment and does not read the
production `.env`. It generates private `compose.yaml`, `telemetry.json`, test
parameter defaults and local state below `.runtime/prosys-test`. Keep these
files private and ignored by Git. Host loopback is translated to
`host.containers.internal` inside containers. Diagnose actual hostname
resolution, ports, routing and field mappings rather than disabling guards.

| Service | Local port |
| --- | ---: |
| Central UI | 18000 |
| Synthetic G | 18001 |
| Controller status | 18002 |
| External process model | 18010 |
| Grafana | 3001 |
| InfluxDB | 8088 |
| RabbitMQ / management | 5674 / 15674 |

The five field names and NodeIds must match the generated configuration.
RabbitMQ uses exchange `idp.bus`: `telegraf.to.process` supplies process
telemetry and `opcua_writer.to.process` supplies command outcomes. Telegraf
must run continuously, not in `--test`/`--once` mode. Never publish Synthetic G
and production image GSensor measurements on the same consumer queue.

## 5. Commission before each dynamic test

First verify Controller and the process model are inactive. Run these actions
separately and inspect every report before proceeding:

```bash
"$MPC_PY" -B -m crystallization_mpc.testing.prosys.commissioning probe \
  --report .runtime/prosys-test/reports/probe-NEW.json

"$MPC_PY" -B -m crystallization_mpc.testing.prosys.commissioning initialize-static \
  --execute-writes --report .runtime/prosys-test/reports/static-init-NEW.json

"$MPC_PY" -B -m crystallization_mpc.testing.prosys.commissioning verify-static \
  --execute-writes --telemetry-config .runtime/prosys-test/telemetry.json \
  --report .runtime/prosys-test/reports/static-verify-NEW.json

"$MPC_PY" -B -m crystallization_mpc.testing.prosys.commissioning writecheck \
  --execute-writes --report .runtime/prosys-test/reports/writecheck-NEW.json
```

Only `probe` is read-only. **`verify-static` also writes**: it requires 60
distinct healthy approximately one-second Telegraf rounds, independently
checks their raw Influx records and quality, changes `T` from 25 to 26 degrees C
(299.15 K), and restores 25. `writecheck` independently reconnects/readbacks a
temporary 37-degree-C setpoint and restores 22. A direct UA read or a graph
with similar values does not satisfy the real Telegraf/Influx round check.

Immediately before **each new dynamic run**, with both services inactive:

```bash
"$MPC_PY" -B -m crystallization_mpc.testing.prosys.commissioning prepare-dynamic \
  --execute-writes --report .runtime/prosys-test/reports/dynamic-init-NEW.json
```

This restores the model's actual initial `T_j_set` to 42 degrees C. A cached
stopped-model status or a reset request alone is not an independent UA readback.

### Short data-link and actual UI checks

The following explicitly starts **one new**, unseeded sigma experiment, keeps
adaptation and Synthetic G off, and observes at most 180 active seconds:

```bash
"$MPC_PY" -B integration_tests/prosys/interactive_short.py \
  --execute-short --mode data-link --active-seconds 180 \
  --commissioning-evidence .runtime/prosys-test/reports/static-verify-NEW.json \
  --telemetry-config .runtime/prosys-test/telemetry.json \
  --output .runtime/prosys-test/reports/short-seed123-NEW
```

Require six consecutive valid committed ticks, all five fresh fields/quality,
correlated Writer verification, subsequent process temperature response, owned
cleanup and five-CSV archive validation. Check `report.json` and
`interactive_short_result.json`. This is a partial no-seed thermal/data-link
test, not post-seed concentration/G, four-stage or hardware acceptance.

After separate dynamic preparation, use another new output and
`--mode ui-parameters` for a human-operated 180-second UI window. The tool does
not edit parameters or click Save. Save real browser Pending/Applied screenshots
and correlate the exact event/revision/effective tick with actual parameter
values, refresh state/P/integrals and the next normal update. Its capture alone
remains `REVIEW_REQUIRED`; button text is not numerical evidence.

`online_parameter_check.py --execute-parameter-test` provides a separate
automated Controller-only fixture (see its `--help`); it creates two owned
experiments, each with a 600-second total fixture budget that reserves 120
seconds for cleanup. It is not a real browser or
production image-parameter acceptance result. Do not mix parameter or fault
checks into the formal experiment.

## 6. Named fault evidence and current runtime qualification

Collect the following as separate, explicitly owned short tests. Each must
restore its configuration, confirm the old run cannot automatically resume,
use End/new-run recovery where required, and retain original archives:

| Qualification case | Test entry point |
| --- | --- |
| `static_read` | commissioning `verify-static` |
| `controller_restart` | `crystallization_mpc.testing.prosys.restart_check` |
| `duplicate_command` | `crystallization_mpc.testing.prosys.duplicate_check` |
| `stale_process` | `crystallization_mpc.testing.prosys.stale_check` |
| `broker_availability` | `crystallization_mpc.testing.prosys.broker_check` |
| `growth_timeout`, `gsensor_restart` | `crystallization_mpc.testing.prosys.growth_fault_check` |
| `model_stop` | `integration_tests/prosys/model_stop_check.py` |
| `native_disconnect` | `integration_tests/prosys/native_disconnect_check.py` |
| `read_only_setpoint`, `missing_node` | `integration_tests/prosys/native_fault_check.py` |

Inspect each entry point's `--help` for its explicit execution flag and ownership
arguments. They may stop services or the exact guarded native process: they are
not passive checks. `native_fixture_tools.py` prepares **new** fault projects
offline; merely creating a project is not live fault/recovery evidence.
Broker availability does not cover an uncertain in-flight command outcome.

### Native fixture portability boundary

The native-disconnect guard intentionally accepts only the approved unchanged
`.runtime/prosys-test/native-fixtures/normal-baseline-001.uasim`, SHA-256
`510160dc21584c4e965920f0e607ec64619eef4170152bb31798c94d020ec993`,
with the native installation layout at `.runtime/prosys-test/application`, its
bundled Java/Install4j classpath and actual port-53530 listener. It verifies the
explicit process owner/PID/start identity before signaling it. Equivalent node
values in a different project do not establish byte identity. The approved
project and vendor installation are **not included** in this repository.

If those prerequisites cannot be independently established on a new machine,
full native qualification remains **NOT RUN / blocked pending a reviewed
new-machine fixture**. Do not replace the hash, remove identity checks, use
arbitrary PIDs or copy another machine's evidence to bypass the guard. Basic
commissioning and short-dynamic checks have their own narrower scope.

### Build qualification from actual local evidence

After collecting the named cases, obtain fresh read-only runtime provenance:

```bash
"$MPC_PY" -B integration_tests/prosys/collect_deployment.py \
  --deployment .runtime/prosys-test/reports/deployment-NEW.json \
  --comparison .runtime/prosys-test/reports/comparison-NEW.json
```

This records the five actual app containers/images and source/config hashes;
it neither deploys nor qualifies them. Review the outputs; do not publish
reports or raw container inspection, which can contain machine/process data.

Explicitly bind **all eleven** cases. Case values below are relative to the
reports directory, not full `.runtime/...` paths. Do not rely on the tool's
historical default attempt names:

```bash
"$MPC_PY" -B integration_tests/prosys/integration_qualification.py build \
  --deployment .runtime/prosys-test/reports/deployment-NEW.json \
  --comparison .runtime/prosys-test/reports/comparison-NEW.json \
  --output .runtime/prosys-test/reports/qualification-NEW.json \
  --case static_read=static-verify-NEW.json \
  --case controller_restart=controller-restart-NEW \
  --case duplicate_command=writer-duplicate-NEW \
  --case stale_process=stale-process-NEW \
  --case broker_availability=broker-availability-NEW \
  --case growth_timeout=growth-timeout-NEW \
  --case gsensor_restart=growth-restart-NEW \
  --case model_stop=model-stop-NEW \
  --case native_disconnect=native-disconnect-NEW \
  --case read_only_setpoint=native-readonly-NEW \
  --case missing_node=native-missing-count-NEW

"$MPC_PY" -B integration_tests/prosys/integration_qualification.py verify \
  --qualification .runtime/prosys-test/reports/qualification-NEW.json --live
```

`build` re-evaluates saved evidence, not missing scenarios; `verify --live`
additionally reads current runtime identities/bytes. Paper launch requires
safety, provenance and runtime PASS and `eligible_for_paper=true`. Some narrowly
recognized intentional acquisition outages can retain artifact FAIL and overall
INCOMPLETE while qualifying safety recovery. Do not relabel them full PASS or
invent a generic ignore-artifacts exception. Missing or changed required
evidence blocks launch. Rebuild/requalify after source/configuration changes.

## 7. One formal run, at most two real hours

Use only a qualified managed entry point after the same-source, same-seed short
dynamic report and CSVs pass, fresh dynamic preparation completes, and relevant
services are inactive. This guide's profile is **one seed-123 experiment**,
`dt=dt_G=15 s`, `sigma_set=0.035`, `G_set=3e-8 m/s` (0.030 micrometres/s),
initial `E_A=150 kJ/mol`, `k_0=2e15`, `n=1.75`.

| Phase | Action / transition condition |
| --- | --- |
| 1. Onset preparation | Sigma control, no seed, adaptation off; positive sigma stays within 0.035 +/-0.005 for 60 s. |
| 2. Preliminary adjustment | Add 1 g seed once, enable Synthetic G, adapt only E_A; four successive changes from new successful fits are <=10%, with k_0/n fixed. |
| 3. Status adjustment | One request switches G control and all three fitted parameters; four successive fit changes <=10%, and each pairwise model/filtered/target G difference <=0.006 micrometres/s for 60 s. |
| 4. Control operation | Continue G/all; finish at a command of -20 to -19.99 degrees C and actual jacket -20 to -19.5 degrees C (with the code's small numerical tolerances), continuously for 60 s, or stop earlier for timeout/fault/operator request. |

Synthetic G has a 120-second enable warmup and is not image-edge filtering;
compatibility u/v fields carry synthetic values. Do not shorten warmup, fit
windows, phase gates or physical limits to reach a desired outcome.

Several low-level/legacy runners still default to **six hours** and the raw
`runner` CLI can start a paper experiment without `--run-paper`. Do not use
those defaults as this formal profile. Always pass **`--max-hours 2`** to
`managed_paper_run.py` and verify the schema-3 plan derives `active_s=7200`.
No automatic second seed, retry or old-run continuation is authorized.

### Detached supervision and deliberate launch

Create a new command JSON with an editor; it is an argv array, not shell text.
Replace both absolute-path placeholders and every `NEW` evidence reference:

```json
[
  "/ABS/CLONE/.venv/bin/python", "-B",
  "/ABS/CLONE/integration_tests/prosys/managed_paper_run.py",
  "--run-paper", "--seed", "123", "--max-hours", "2",
  "--integration-qualification", ".runtime/prosys-test/reports/qualification-NEW.json",
  "--commissioning-evidence", ".runtime/prosys-test/reports/static-verify-NEW.json",
  "--dynamic-evidence", ".runtime/prosys-test/reports/short-seed123-NEW/report.json",
  "--telemetry-config", ".runtime/prosys-test/telemetry.json",
  "--output", ".runtime/prosys-test/reports/paper-seed123-NEW"
]
```

Save it as `.runtime/prosys-test/reports/command-NEW.json`, then plan only:

```bash
"$MPC_PY" -B integration_tests/prosys/managed_unit.py plan \
  --command-json .runtime/prosys-test/reports/command-NEW.json \
  --working-directory "$MPC_REPO" \
  --output .runtime/prosys-test/reports/plan-NEW.json
sha256sum .runtime/prosys-test/reports/plan-NEW.json
```

Review the exact command, source files, unit and budgets before approving launch.
Set `MPC_PLAN_SHA` to the hash just inspected, then launch **once**:

```bash
"$MPC_PY" -B integration_tests/prosys/managed_unit.py launch \
  --manifest .runtime/prosys-test/reports/plan-NEW.json \
  --manifest-sha256 "$MPC_PLAN_SHA" \
  --intent-output .runtime/prosys-test/reports/intent-NEW.json \
  --output .runtime/prosys-test/reports/launch-NEW.json --execute-launch
```

The 7200-second activity cap starts at Controller's acknowledged experiment
start, not the end of warmup or a phase change. Startup/cleanup/report/finalize
budgets remain 180/300/60/30 seconds. Outer `RuntimeMaxSec=7770` triggers stop;
`TimeoutStopSec=390` is **additional** grace, not a guaranteed total completion
time. Power loss or uninterruptible I/O can prevent graceful completion.
Record actual stop/archive times. A late/uncertain launch result requires
identity reconciliation, never another launch attempt.

## 8. Stop, archive and interpret results

After launch, hash the **intent** and **launch-result** files. Set
`MPC_INTENT_SHA` and `MPC_LAUNCH_SHA` from those exact receipts. Subsequent
status/stop use the intent, **not the plan**, as their manifest:

```bash
"$MPC_PY" -B integration_tests/prosys/managed_unit.py status \
  --manifest .runtime/prosys-test/reports/intent-NEW.json \
  --manifest-sha256 "$MPC_INTENT_SHA" \
  --launch-result .runtime/prosys-test/reports/launch-NEW.json \
  --launch-result-sha256 "$MPC_LAUNCH_SHA" \
  --output .runtime/prosys-test/reports/status-NEW-01.json

"$MPC_PY" -B integration_tests/prosys/managed_unit.py stop \
  --manifest .runtime/prosys-test/reports/intent-NEW.json \
  --manifest-sha256 "$MPC_INTENT_SHA" \
  --launch-result .runtime/prosys-test/reports/launch-NEW.json \
  --launch-result-sha256 "$MPC_LAUNCH_SHA" \
  --output .runtime/prosys-test/reports/stop-NEW.json --execute-stop
```

Stop only the exact owned invocation. A successful stop request is **not**
proof of termination. Save a fresh status capture and confirm Controller
inactive, experiment ended, owned plant stopped and five CSVs validated.
Without a managed unit, use Central's End Experiment workflow, verify Controller
inactive/ended, and reconcile/stop only the matching plant identity. Never treat
container shutdown as experiment finalization. Once no experiment is active,
the following stops the isolated stack without deleting volumes:

```bash
"$MPC_PY" -B -m crystallization_mpc.testing.prosys.deployment stop
```

Do not purge queues, remove volumes, delete reports or restart an old experiment
to manufacture a pass. A G timeout or a genuine service restart requires manual
End and a distinct new run. Missing G is not zero G; parameter refresh does not
clear the established stream watchdog (`2 * current dt_G`).

Read `report.json`, `qualified_report.json` and `managed_report.json` separately:

- Integration, procedure/gates, filter refresh, control behavior, hardware and
  archive integrity have different verdicts. Five valid CSVs do not prove control.
- Two hours without the jacket endpoint is INCOMPLETE, unless an earlier failure
  already applies. Never turn an earlier FAIL into ordinary timeout.
- Operator interruption is recorded explicitly; the current runner may report
  FAIL for `operator_interrupted`. That is not evidence of a control defect.
- `REVIEW_REQUIRED` is not PASS; inspect full-run error, deviation duration,
  command limits and endpoint, not just the last G value. Supervisor completion
  and a systemd success result are not experiment verdicts.
- Keep actual UTC timestamps, period/tick identities and native report contents.
  Filter Grafana by this run and actual start/end; preserve raw gaps, don't
  rescale time or invent missing diagnostics. Save overview, refresh-boundary,
  covariance/innovation and requested/final/actual jacket views as local evidence.

## 9. Production GSensor parameter acceptance remains separate

Synthetic G can qualify Controller-only updates, not `dt_G`, resolution,
alignment or image-processing changes. A separate experiment must use the
production `crystallization_mpc.apps.gsensor.app:web_app`, real identified images
at the declared cadence, confirmed baseline/corner/3D markings, independently
calibrated resolution and the selected alignment method's actual dependencies.

Correlate Save Parameters -> prepare -> Controller effective tick/commit ->
GSensor frame-boundary applied -> final Applied, including run/start/event,
revision, measurement revision and `effective_after_frame_seq`. Verify actual
new parameters and subsequent new-version u/v/G through Controller and archive;
the first re-anchoring frame alone is not valid G. Test duplicates, invalid/stale
requests, replay isolation, retained startup draft and clean finalization.

Do not erase or relax the unchanged G watchdog to pass. A declared Disable ->
update -> Enable -> warmup procedure is a planned measurement pause, not proof
of seamless updating while enabled. `ptr_format` and `params_G.ratio` are
currently metadata-only; don't claim they change numerical image processing.
Until production image/protocol evidence exists, keep this scope NOT RUN even
if model loading, isolated unit fixtures and Controller parameter checks pass.

## 10. Publication and local-only evidence

The reviewed support and 13 host tools are the explicit distribution exception
to the repository's local-test policy. `.gitignore` allowlists those host files;
other regression tests, research tools and generated files remain local. Use
the included `--help` parsers to inspect interfaces without starting a test.
Local offline regressions, where separately available, use fakes/temporary
evidence and never replace native live qualification.

Publish source and this generic procedure only. Keep `.runtime`, credentials,
certificates/private keys, `.uasim` projects, installers, captured image sets,
raw container inspections, database state, runtime plans/receipts, reports,
screenshots and CSVs out of Git. Supply any machine prerequisites separately
under appropriate access and licensing rules, and independently review them.
This document makes no inherited PASS or completed two-hour experiment claim.
