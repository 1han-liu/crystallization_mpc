# Central runtime controls: implementation and acceptance record

> Distribution note: tests, reference fixtures, and diagnostic/demo scripts are
> local-only and excluded from Git. Test/demo commands and paths below document
> the development checkout; they are not available in a fresh clone without
> those local files. Production runtime and startup tools remain tracked.

## Objective and boundary

Within one experiment, reuse Central's control target, adaptive parameter
selection and adaptation enable controls. Add independent positive sigma/G
setpoints. Submit on selection changes, and on Enter/blur for numeric inputs.
Confirm actual Controller application before showing success. Preserve numerical
state, startup defaults, existing MATLAB adaptation policy and all GSensor code.

The authoritative objective is the user's Central runtime-controls long-term
plan. This document records progress; it does not reduce its acceptance scope.

## Current automatic-refresh protocol (2026-10-09)

This section supersedes the historical state-preserving-without-refresh behavior
documented below. MATLAB is a read-only reference; all implementation changes
are confined to MPCrystal_final. Simulation and Experiment share one numerical
refresh implementation; only the source of fresh temperature/concentration differs.

- A successful actual change of control target, adaptation enablement, adaptive
  mode, sigma_set or G_set stages one pending request. Standby setpoint changes
  and mode changes while adaptation is disabled also refresh. A batch refreshes
  once. No-op values, invalid/rejected/conflicting requests, repeated event IDs,
  polling and automatically fitted kinetic parameters do not refresh.
- The actual configuration and revision remain unchanged until the next control
  tick supplies valid fresh T/c. That tick reconstructs the five Controller EKFs,
  initializes the main state to [T, 0, c, 0], clears three integrals, holds the
  previous jacket command and starts a new fitting segment. It does not perform
  an extra EKF prediction or advance the simulated plant twice.
- Historical measurements and cumulative fit/failure counts are retained. Only
  eligible post-refresh samples enter the active bounded fitting window. Missing
  samples are not zero growth, and derivatives do not cross gaps or refreshes.
- Run identity, startup reference, physical state, seeds and fitted kinetic
  parameters are preserved. No GSensor filter, annotation or tracking state is
  reconstructed by these five Controller configuration operations.
- Status exposes pending/completed/failed refresh, event ID, count, segment and
  actual effective tick. Central explicitly reports unchanged values without
  refresh and new-segment sample accumulation. Durable applied acknowledgment
  follows a successful checkpoint save; storage failure stops Controller without
  claiming success. Pending requests and applied IDs survive a compatible restart.
- Older checkpoints missing this required numerical state are rejected and
  preserved, not populated with invented state or overwritten automatically.
  Start a separate new session when upgrading from such an archive.

### Four-phase numerical acceptance

Use the isolated accelerated driver with Simulation + Simulated, MPC, dt=dt_G=15s,
sigma_set=0.035, G_set=3e-8m/s, E_A=150000J/mol, k_0=2e15m/s, n=1.75 and 1g seed.
These are per-run overrides; production default dt=5s and physical _proc
kinetics/actuator limits are not changed. All timing gates use simulated elapsed
seconds. Do not use the cleanup launcher for this acceptance.

1. Adaptation off, no seed: sigma positive and within 0.005 of 0.035 for 60s.
2. One confirmed seed addition, remain in sigma, enable E_A only: accumulate
   eligible samples and require four successful updates changing E_A by <10%,
   with k_0/n fixed. A flat line without successful fitting is not convergence.
3. In one request switch to G and all three adaptive parameters: exactly one
   refresh; new samples must accumulate. Require four successful stable updates
   (<10% each) and 60s with both calculated/filtered-measured G within 20% of the
   target and their difference within 20% of the target (6e-9m/s).
4. Retain G/all adaptation to the unchanged jacket lower limit: setpoint within
   0.01K and actual jacket within 0.5K continuously for 60s. Non-actuator-limited
   tracking failure sustained for 60s is a FAIL; limit-induced error is reported
   separately. Stop by 120 simulated minutes; do not force phase transitions.

The paper's 0.03 prose vs 0.035 figure discrepancy uses the approved figure value.
Reactor 15C is not the endpoint. Published final parameters and 80-minute duration
are references, not hard-coded expected outcomes. Image warmup (>8 frames), FBRM,
physical seed dissolution, nucleation and size-distribution control are NOT RUN
in a numerical-source acceptance. All historical test totals below refer only
to their original dates; current evidence is produced by this new run.

## Baseline (2026-09-10)

- Checkout: `final`, `06b5c5ea04c45122101be91f1c09563b989f972b`.
- Before these edits: `.venv/bin/python -m pytest tests/controller -q --disable-warnings`
  produced **201 passed, 13 failed** in 12.64 seconds.
- All 13 failures are in
  `test_adaptation_reference_strategy.py::test_reference_numerical_tolerance`:
  positive/E_A_and_k_0, positive/all, negative_integer/E_A_and_k_0,
  tiny_negative/{E_A_and_k_0,E_A_and_n,k_0_and_n,all},
  zero/{E_A_and_k_0,E_A_and_n,k_0_and_n,all},
  window/{E_A_and_k_0,all}.
- These are existing optimizer numerical differences, not runtime-control
  regressions. Do not relax their tolerances or change the optimizer for this work.
- Existing local edits in adaptation.py, controller.py and their MATLAB reference
  documentation/tests are preserved. Existing simulation scripts and fixtures
  were already untracked. No experiment data was changed.

## Implementation checkpoint (2026-09-10)

- Shared contract and state-preserving algorithm hook: implemented and tested.
  The hook updates only the runtime whitelist. It does not initialize a new run,
  clear integrators/EKF/history, or change the original parameter digest.
- Controller command/revision/idempotency/audit/recovery: implemented and tested,
  including simultaneous old-revision requests and updates waiting on a tick.
  Runtime event results are journaled in the existing Controller state document;
  optional fields preserve compatibility with old Controller archives.
- Central API and per-run confirmation/retry: implemented, covered by HTTP tests
  using the real Python algorithm and an **in-process transport, not RabbitMQ**.
  Requests are saved before delivery in `.central_runtime_requests.json` under
  the configured experiment root. The existing adaptation endpoint delegates to
  the runtime path; it still returns an event ID for old clients.
- Actual-target telemetry and adaptation diagnostics: implemented and tested.
  Parameter adaptation success is reported only after fitting actually runs.
- Seven adaptation combinations and MPC/PI state-preservation/recovery checks:
  tested. Tests use c_init=0.4 only as an isolated valid-input fixture; production
  defaults and the existing negative-supersaturation failure remain unchanged.
- Earlier targeted suite: **82 passed**, 1 dependency warning, in 5.81 s.
  Command:
  `.venv/bin/python -m pytest tests/central tests/controller/test_runtime_updates.py tests/controller/test_runtime_service.py -q --disable-warnings --junitxml=.runtime/central-runtime-controls/new-tests.xml`
- Latest existing Controller + new Central suite:
  **286 passed, same 13 pre-existing failures**, no additional failures (57.33 s),
  including both new four-phase demo regression cases and a storage-error test.
  XML evidence: `.runtime/central-runtime-controls/final-tests.xml`.
- `git diff --check`: PASS.

## UI and numerical checkpoint (2026-09-10)

- Existing HTML/JS/CSS now reuse the original three controls; Adaptive Mode is
  three checkboxes with seven combinations. The lower duplicate adaptation
  action is removed. Two independent positive setpoints submit on Enter/blur.
- Runtime changes are immediate requests, not optimistic success. Pending edits
  lock only runtime editing while status polling continues. The same event is
  retained across reloads/retries. A stale numeric draft or old revision is
  rejected; Controller settings remain authoritative. Startup setpoints use the
  existing pre-run parameter-save path, not the runtime endpoint.
- Fitting enablement and actual fitting diagnostics are separate. Waiting,
  successful and failed fitting are displayed with tick/mode/count information.
- **10 real Chromium UI scenarios PASS**, using mocked HTTP responses (not a
  RabbitMQ acceptance). Covers seven combinations, initial/runtime paths,
  pending/reload/retry, invalid inputs, one Enter+blur submission, stale edits,
  offline/wrong-run Controller and explicit rejection/timeout.
  The additional scenario distinguishes actual application from failed local
  persistence: **active — not saved** must not be shown as a normal saved result.
  A successful later save clears the warning; fitting and control are unchanged.
- Browser test command (local bundled dependency path):
  `NODE_PATH=/home/laniakea/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node --test tests/central/runtime_ui.test.cjs`.
  It uses an isolated headless Chrome context, never the user's browser profile.
  Screenshot: `.runtime/central-runtime-controls/ui/runtime-controls.png`.
  Visual inspection caught and fixed a CSS rule exposing a hidden retry button.
- Four-stage numerical demo: **MPC PASS, PI PASS**, 80 ticks each, each with
  10 exact recovery-output comparisons. At ticks 50/60/62/68/70/80, the MPC
  completed-fit counts are 11/21/23/23/25/35: fitting freezes while disabled
  and resumes when enabled. All output-validity, finite-value, target-gradient,
  jacket-temperature, startup-state and runtime-state-preservation checks pass.
- Evidence: `.runtime/central-runtime-controls/four-phase-20260910-01/{MPC,PI}/report.json`
  and `cycles.csv`. These are real numerical simulations with in-process commands,
  not network or real-equipment experiments.
- The repeatable demo is also covered by `tests/controller/test_runtime_demo.py`.

## Real browser and RabbitMQ acceptance

The final isolated run used real Chrome, Central HTTP, a unique RabbitMQ
exchange/two queues, and the real numerical Controller. No HTTP responses were
mocked. The driver exercised the original Central page with Playwright.

Evidence: `.runtime/central-runtime-controls/browser-20260910-final/report.json`,
`browser-report.json` and `runtime-browser.png`.

Seven acceptance checkpoints:

1. A real Simulation + Simulated run starts and only the intended runtime controls unlock.
2. Browser target, independent setpoint, parameter-combination and adaptation
   on/off changes travel over RabbitMQ and receive actual Controller revisions.
3. Reload displays actual values, not the unchanged startup configuration.
4. A deliberately broken **test publisher connection** leaves a pending request;
   restarting Central preserves it; retry reuses the event ID and applies once.
   Replaying the acknowledged request does not increment the revision again.
5. Stopping only the test Controller locks editing, without fake success.
6. A new Controller process restores the same run, revision, settings and tick state.
7. A subsequent real numerical output is valid, uses the current target/setpoint
   and revision, and records zero OPC UA reads/writes. Startup files are unchanged.

The RabbitMQ container was not stopped. Only this runner's temporary queues,
exchange and processes were removed afterward; reports and experiment evidence
were retained. Ports 8000/8001 and existing experiments were left untouched.
The isolated Central manifest can remain `starting` because no GSensor is part
of this test; actual Controller `running`, revision acknowledgments and outputs
are verified separately. This is not full-platform/GSensor acceptance.

Early `browser-20260910-01` through `04` runs exposed mistakes in the new test
runner's setup/assertion field names. They were corrected without changing the
production startup contract; failed artifacts remain available. Run `05` also
passed, followed by the final run with explicit output and no-device assertions.

Repeat from the project root, while the configured development broker is available:

```bash
.venv/bin/python scripts/run_runtime_browser_acceptance.py
```

It reads only RABBIT_URL from `.runtime/rabbitmq-debug/runtime.env`, without
evaluating shell code or printing credentials. Chrome and Node Playwright are
required; paths are overrideable with `--chrome` and `--node-modules`. It creates
a fresh timestamped output directory and never reuses an existing one. Private
service logs have mode 0600. Test-only fault-injection routes exist only in this
runner's isolated Central process, never in the production entrypoint.

## Final requirement audit

| Requirement | Verification | Result |
|---|---|---|
| Reuse three controls; no duplicate adaptation action or Apply button | Chromium UI and screenshot | PASS |
| sigma/G round trip; active and standby setpoints; finite positive validation | runtime_updates + HTTP/UI tests + real broker run | PASS |
| Three checkboxes, seven modes, minimum one; off-state selection | seven-mode parameterized algorithm and browser tests | PASS |
| On/off preserves learned parameters/history; enable is not successful fitting | real fitting diagnostics and four-phase MPC/PI demos | PASS |
| Immediate submission; Enter/blur only once; pending controls lock | HTTP/UI scenarios | PASS |
| Atomic between-tick updates; correct target aliases; no reset of state/startup timing | state snapshot and concurrency tests | PASS |
| Per-run/version/identity guards, unsupported adapter, rejection/timeout/retry | runtime service, HTTP and browser tests | PASS |
| Real browser → HTTP → RabbitMQ → actual Controller | isolated browser acceptance | PASS |
| Pending Central restart and actual Controller process recovery | real restart tests and exact numerical recovery comparisons | PASS |
| Old optional-field-free Controller archives remain readable | test_old_archive_without_runtime_fields_is_still_recoverable | PASS |
| Actual configuration in output/telemetry and next-run defaults isolated | telemetry test, numerical CSV and real browser assertions | PASS |
| Local storage failure is visible, not a false durable-success claim | injected os.replace failure + browser warning test | PASS |
| MATLAB strategy preserved, existing failures not hidden | same 13 baseline failures; default-domain failure reproduction | PASS for non-regression, existing parity failures remain |
| GSensor unchanged; no experiment-image deletion or device access | scoped diff and isolated runner/output device counters | PASS |
| Physical experiment | outside this software acceptance | NOT RUN |
| Git push / PR / production-service restart | not authorized | NOT RUN |

The full Python suite is **not all green**: its 13 pre-existing MATLAB tolerance
failures remain FAIL. Default-input adaptation domain failure also remains a
known separate issue; passing its failure-reporting test does not make that
default simulation successful. These are not hidden by the valid-input c_init
fixture and were not changed by this runtime-control feature.

Deployment assumption: one Central process owns its filesystem request journal;
the in-process lock is not a multi-worker/distributed transaction mechanism.
The operator UI must not be deployed with multiple independent Central workers
against one journal without adding shared concurrency control in a separate task.

## Repeat the numerical demo

From the project root:

```bash
.venv/bin/python scripts/run_runtime_control_demo.py
```

This creates a fresh timestamped directory beneath
`.runtime/central-runtime-controls/`, preserving existing runs. It requires no
images, RabbitMQ, database or real devices. It uses c_init=0.4 and sigma_set=0.035
only for its documented valid-input test fixture; no production default changes.
Control dt stays 5 seconds while simulation advances without real-time waiting.
Use `--mode MPC` or `--mode PI` to select one configured controller mode.

After the software is deployed/restarted, operate Central's Run Configuration
panel: select the target, select adaptive parameters, and enable/disable
Adaptation. Changes are per-run. Read the confirmation label before continuing.
For an unconfirmed request, use **Retry same request**, not a new experiment.
Never interpret enablement alone as a successful fit or a safe physical run.
The currently running 8000/8001 services have NOT been restarted by this work.

## 操作说明：你的三个 Central 项

### 开始之前

本轮没有重启正在使用的 8000/8001 服务，也没有部署。以后启用本版本时，
Central 和 Controller 都必须加载更新后的代码，并使用同一实验和正确消息配置。
仅刷新网页不能更新已运行的 Python 后端。GSensor 无需因这三个功能而修改。

无设备演示请选择 `Simulation + Simulated`，并明确关闭 Controller OPC UA
读写。单纯改成 Simulation 不应代替设备读写开关检查。
使用独立演示实验；不要清空、重置或覆盖现有实验和图片。

### 运行时怎么操作

1. 等待 Central 的 Controller 状态为本实验的 `running`。
2. **Control Target** 单选 sigma 或 G。两个目标值独立保存；数值框按 Enter
   或离开输入框提交。修改备用值不会立即切换目标。G 单位为 m/s。
3. **Adaptive Mode** 勾选 E_A、k_0、n 的组合，至少留一个。勾选只决定哪些
   参数允许拟合，不会自动启用适应，也不是改成新的联合优化器。
4. **Adaptation** 选择 Enabled/Disabled。关闭后控制仍继续，已有参数和历史
   保留。重新启用后按原 MATLAB 策略继续等待样本或拟合。
5. 每一步先等状态确认再继续。`applied` 显示 revision 和 effective tick；
   下方 Adaptation 状态区另看 completed fits / failures，不能把 Enabled
   当作拟合成功。时间、晶种、EKF和已估计参数不会因正常切换而重新初始化。

### 状态含义和异常处理

| 状态 | 应如何理解与处理 |
|---|---|
| awaiting confirmation | 尚未确认；不要当作已生效，也不要假定未生效 |
| applied | Controller 已应用，从标出的后续控制周期使用新配置 |
| rejected | 请求被拒绝，读取原因；不要用旧页面反复覆盖当前配置 |
| confirmation timeout | 仍未确认；恢复连接后使用 Retry same request，不另造重复请求 |
| unconfirmed | Controller 离线、停止或不属于本实验，编辑锁定 |
| active — not saved | 内存配置已生效，但存档失败；先排查存储，不能保证重启恢复 |

多窗口同时操作时，旧版本编辑会被拒绝；刷新后以 Controller 当前值为准。
切换期间 pending 控件锁定是保护行为，不是界面卡死。

### 四阶段演示顺序

准备：sigma + 适应关闭 → 模拟加晶种后只启用 E_A → 切到 G，依次勾选
k_0、n → 保持 G，改变 G_set，关闭再开启适应 → 最后切回 sigma。
本功能不自动判断论文各阶段的结束条件，也不自动调度阶段。
论文 0.03/0.035 的不一致没有用于修改生产默认值。

要快速复核这套过程，运行上面的 `run_runtime_control_demo.py`：自动推进固定
dt 的 80 个数学周期并生成逐周期 CSV；它不需要图片、浏览器或真实设备。
真实浏览器脚本则负责网络/操作/故障恢复验收，不能替代长序列拟合测试。

## 修改位置与队友边界

- `src/crystallization_mpc/apps/central/ui/`：原界面、运行时 API、确认展示。
- `src/crystallization_mpc/apps/central/runtime_controls.py`：Central 请求存档。
- `src/crystallization_mpc/messaging/controller_runtime.py` 和 `commands.py`：专用命令契约。
- `src/crystallization_mpc/apps/controller/{adapter.py,service.py,telemetry.py}`：接收、执行、回执、恢复与记录。
- `src/crystallization_mpc/apps/controller/algorithm/{controller.py,integration.py}`：状态保留式配置更新和拟合诊断。
- `tests/central/`、`tests/controller/test_runtime_*.py`、两个 runtime 演示脚本：验证。

GSensor 开关、时间戳、对齐切换、重标注、重定位及图像中间结果仍归队友。
本次不新增 GSensor 控件/命令，不更改其存档格式。已有适应参考文档、
adaptation.py 和 MATLAB 测试中的工作区改动属于本任务之前的工作，未回滚。

## Runtime API contract

`POST /api/operation/controller/runtime` accepts exactly:

```json
{
  "run_id": "the-current-experiment-id",
  "event_id": "one-unique-id-per-operator-change",
  "expected_revision": 0,
  "changes": {"control_target": "G"},
  "requested_at": "2026-09-10T12:00:00Z"
}
```

Editable keys are control_target, sigma_set, G_set, adaptation_enabled and
adaptation_mode. Both setpoints must be finite positive JSON numbers. The
controller applies each event between ticks and returns its effective_tick.
Polling exposes configuration, revision, last_result and recent_events.
Central exposes the persisted runtime_request separately; it becomes applied or
rejected only when a matching Controller result is observed. An unconfirmed
request times out after 30 seconds and blocks a *new* event, but allows retrying
the same event. A transport exception is explicitly unconfirmed, not a rollback.

The UI must show numerical progress using controller.adaptation.fitting:
enabled, mode, sample_count, minimum_samples, last_status, last_mode, last_tick,
fit_count, failure_count and pause_reason. These are diagnostics only; they do
not alter the MATLAB adaptation strategy.

## Run-local parameter drawer updates

The existing **Save Parameters** button has two deliberately different uses:

- Before a run, it saves the next-experiment draft using the existing complete
  `/api/params` request. Starting an experiment still freezes its startup snapshot.
- During a supported running experiment, it submits only manually edited fields
  for that experiment. It never overwrites the next-experiment draft or startup
  snapshot. **Reset to Defaults** and per-field default-reset controls are disabled.

New startup snapshots additionally record all 91 fields in
`raw_parameter_sections`, because the older target-specific derived preview
omits some standby-target settings. A STARTING retry compares the complete
prepared raw sections as well as the existing snapshot fields; a same-version
external draft change is rejected before any message is published. Valid retries
publish the exact recorded raw sections. Old snapshots are not modified or
given invented raw history; their startup retry retains the legacy flat protocol.
For such legacy flat-only runs, the complete parameter drawer stays locked with
Controller's `unavailable_reason`; ending the old run and creating a new run
provides the required complete startup sections. The original five runtime
controls remain available and preserve their existing behavior.

The closed parameter-policy registry covers all 91 raw fields in Shared,
Gsensor, and Controller. Initialization-only fields stay locked. Calculated
values remain read-only; unused and reserved settings explain their actual
effect rather than claiming that a new calculation has been enabled. For
`dt_G`, the operator must separately match the external camera interval: a
parameter update does not change an external camera's capture rate.

The runtime branch of `POST /api/params` accepts:

```json
{
  "expected_run_id": "the-current-experiment-id",
  "experiment_started_at": "2026-10-20T09:00:00Z",
  "event_id": "one-unique-id-per-batch",
  "expected_revision": 0,
  "requested_at": "2026-10-20T09:01:00Z",
  "changes": {
    "controller": {"params.E_A": 130000, "params.n": 1.6}
  }
}
```

Central publishes `experiment.parameters.update` through the existing broker.
The request shares the original five runtime controls' revision, pending slot,
durable journal, and retry identity. A network error or delayed confirmation is
not an Applied result and is not proof of rollback. Retries preserve the exact
event, including its original timestamp. Only a matching final acknowledgment
from Controller can mark the aggregate operation Applied, after other affected
participants have confirmed their work.

`GET /api/params` keeps the existing sections and metadata. During a running
experiment it overlays confirmed current values from Controller's
`run_parameters` status and adds policies, startup values, and transaction
status. Automatically fitted E_A/n values update unedited inputs; polling never
overwrites a local unsaved edit. If another runtime operation changes the
revision, **Review Current Revision** retains the proposed edits, displays the
current active values, and requires a conscious review before Save Parameters
can submit again. It does not submit or apply a transaction itself.

When a run ends or the selection returns to a non-running experiment, Central
reloads the persisted startup draft before enabling Save/Start. A failed reload
keeps these actions locked and is retried on subsequent status reads. Run-only
values and retained local edits must not silently become the next run's inputs.

UI states distinguish **Unsaved changes**, **Pending**, **Waiting for fresh
measurement**, **Applied at tick …**, and **Failed**. The main-page Adaptive
Mode checkboxes continue to select which parameters may be fitted; they are
not numerical E_A/k_0/n input fields. Manually editing numeric E_A/n does not
disable their existing adaptation selection.

The Development panel **Startup & Runtime Derived Values** separates
`startup_preview` from `current_runtime_derived`. The latter is copied from the
confirmed algorithm status, including its actual fitted k_0, time constants,
and active target aliases. It is read-only; the UI does not recompute k_0 from a
new E_A. `/api/params` exposes these actual `derived_values` and read-only
`derived_policies`. `preview_publish_payload()` retains its startup semantics.

Parameter applications use the existing `controller_runtime_event` Grafana
annotation measurement. The event text preserves section-qualified raw keys,
effective tick, revision, and nested parameter changes. A pending transaction
does not create a successful-application annotation; missing previous values
are not reconstructed from startup defaults.

CSV export keeps startup snapshot rows and adds one row per changed
`section.raw_key`, with the actual revision, Controller effective tick, GSensor
effective frame, and measurement revision where recorded. Aggregate pending or
rejected operations are not marked `is_effective`; partial Controller commits
retain their separate `controller_applied` evidence. Missing historical
revision/frame fields are left blank, not reconstructed as revision zero.
Controller/GSensor CSVs keep any recorded revision fields as ordinary extra
columns. No historical run is rewritten merely to add this metadata.

Isolated Central acceptance is covered by
`tests/test_central_online_parameters.py`, including real HTTP handlers,
durable journals, retries, conflicts, draft immutability, startup/derived-field
rejection, nested annotations, and unsaved-edit/fitted-value UI behavior. These
tests do not start RabbitMQ, Prosys, cameras, or a hardware experiment. The
earlier long-experiment results in this document are historical evidence and
are not an acceptance claim for the new online-parameter functionality.

### Controller and image-processing transaction details (2026-10-20)

The sidebar's numerical E_A/n inputs are distinct from the main page's Adaptive
Mode checkboxes. The former submit explicit values with **Save Parameters**;
the latter still automatically select which parameters may subsequently be
fitted. Neither operation silently changes the other selection.

- Controller keeps the immutable flat `parameters` and original three raw
  `parameter_sections` separately from the run-only overlay and its journal.
  Current read-only values include the latest fitted kinetics. A patch applies
  only its submitted fields; it never rebuilds k_0 from a startup formula.
- `experiment.parameters.prepare`, `.commit`, `.abort`, and `.result` coordinate
  GSensor over the existing RabbitMQ routes. Receipts match the exact run/start
  identity, event, revision, nested values, measurement revision, and frame
  boundary. Preparation is durable but does not activate the candidate.
- A computational update reconstructs all five Controller filters once from
  the current measurement, clears the three integrals, starts a fitting segment,
  and holds the existing jacket command for that tick. An unused-only or
  next-seed-only update records its actual effect without pretending to reset
  filters. The three reserved parameter filters remain unused by the estimator.
- A measurement update cuts the old G cache without erasing the independent
  stream watchdog. GSensor retains marks, processed-image history and global
  counts. Its first new distance baseline is not an effective G measurement.
  No missing G is replaced with zero. Ordinary Controller-only edits do not
  reconstruct image filters.
- The existing GSensor alignment UI remains usable. Its validated status
  reports the actually confirmed method, which the drawer observes separately
  from the immutable parameter journal. Switching there and then selecting the
  original method in the drawer is an actual change, not a false no-op.
- Controller revision and numerical refresh count are deliberately different:
  metadata-only applications use a revision without a refresh. Cross-module
  transactions remain Pending after Controller's durable decision until the
  matching GSensor receipt. While awaiting a computational/measurement update's
  receipt, optimization, integration and fitting are held; fresh main-state
  estimation may continue. A metadata-only receipt does not pause control or
  claim a filter refresh.
- A lost request or receipt is retried with the original event ID. A failure
  before Controller commit leaves its old configuration. An explicit participant
  rejection after commit is reported as a **partial update, not a rollback**;
  control stops and the operator must end that run. Uncertain receipts are not
  converted to Applied just because a revision increased.
- On a `dt` change, the application tick uses the old interval and continuous
  accumulated elapsed time; only subsequent deadlines use the new interval.
  `dt_G` controls measurement calculations and freshness, not an external
  camera's capture rate. The established-G timeout remains twice the active
  `dt_G`; expiry retains the manual-End/new-run requirement.
- New jacket bounds must fit the existing Writer envelope and include the
  command held during refresh. Candidates are rechecked at the fresh control
  boundary; a newly incompatible candidate is rejected without applying it or
  running the numerical cycle twice.
- Internal Simulation rejects candidates above **1,000,000 simulated crystals**
  or **100,000 jacket integration substeps per control cycle** before allocating
  or stepping them. These are computational resource limits, not new physical
  danger thresholds. Requests are rejected, never silently clipped. Such
  internal parameters do not rewrite a Prosys or physical plant.

Checkpoints validate startup plus the accepted patch journal, applied algorithm
receipts, filter refresh counts and continuous time. The accepted patch can be
a proper subset of the request when another requested value already matches the
latest fit. A pending patch must also match its original operator request; two
mutually consistent but altered copies are insufficient. Restart never arms a
control deadline or automatically commits a prepared image update.
New startup snapshots also retain all 91 raw fields, including standby-target
fields omitted by the historical flat message. A STARTING retry cannot replace
these with a changed same-version draft. Older snapshots without those full raw
sections remain on the original startup/five-controls protocol; the parameter
drawer stays unavailable for that legacy run instead of inventing GSensor's
startup settings. End it and create a new run to use the expanded drawer.

Controller telemetry retains `runtime_revision`,
`measurement_parameter_revision`, and, when supplied by a sample,
`input_parameter_revision`. GSensor records carry `parameter_revision`.
Parameters CSV retains section-qualified keys, actual revision, effective tick
and frame, measurement revision and `controller_applied`; a partial outcome is
not presented as a fully applied transaction. Older missing revision values are
left missing rather than backfilled. No new Grafana panels are introduced.

### Isolated verification and screenshots

Current offline transaction regressions are in:

- `tests/test_online_parameter_policy_algorithm.py`: the closed 91-field policy,
  typed values, dependencies, exact initialization matrices, manual kinetics,
  unchanged-run numerical/call-count comparison, variable periods, seed
  populations, resource limits and checkpoint replay.
- `tests/test_online_parameter_service.py` and
  `tests/test_online_parameter_review.py`: actual Controller application ticks,
  competing queues, deferred requests, rejected/partial/uncertain results,
  persistence failures and independent corruption probes.
- `tests/test_gsensor_online_parameters.py` and
  `tests/test_online_parameter_cross_module.py`: actual Controller/GSensor
  services with an explicit in-memory message pump, all four transport-loss
  boundaries, frame-boundary serialization, real EKF warmup fixtures, stale
  versions, restarts and the unchanged established-stream timeout latch.
- `tests/test_central_online_parameters.py`,
  `tests/central_online_parameters_browser.cjs` and
  `tests/test_online_parameter_export.py`: HTTP, durable requests, actual
  Chromium rendering/clicks, retained drafts, next-run isolation and CSV events.
- `tests/test_online_parameter_http_flow.py`: full HTTP creation/configuration/
  startup through real Central, Controller and GSensor application classes with
  an in-memory bus; actual receipts drive Applied, standby values survive the
  old flat wire, and interrupted startup retries cannot alter the full frozen
  snapshot. No external network connections are allowed in these scenarios.

As specified by this repository's existing distribution policy, local tests and
fixtures remain excluded from Git; no tests were silently added to a push.
Verification records and real rendered screenshots are retained locally in
`.runtime/online-parameters-validation-20261020-pWzPT4CL/`. The screenshots are
explicitly labeled **Isolated UI fixture**; HTTP responses are intercepted in
the browser test, and they are not Prosys or hardware evidence. The Controller
and GSensor cross-module tests use their real application classes with an
in-memory bus and deterministic image/optimizer fixtures. They do not claim
real RabbitMQ/network, real-camera or full crystallization-process acceptance.
No stopped long experiment was resumed, no historical data was cleaned, and
no push was performed for this implementation.

Deployment is deliberately separate from these isolated checks: Central,
Controller and GSensor must all load the updated source. No already-stopped
test, long Prosys run or hardware experiment is automatically started or
resumed. A legacy run lacking full startup sections must be ended before a
new run can use the expanded parameter drawer.

### Final isolated verification record (2026-10-20)

The final combined run completed with **356 passed, 120 subtests passed,
30 skipped, 2 warnings**, and no failures or errors (32.97 seconds).
The 30 skipped cases are the pre-existing explicitly opt-in local InfluxDB
Flux round-trip checks, not skipped online-parameter assertions. They were not
enabled as a substitute for hardware acceptance. Python compilation, JavaScript
syntax and `git diff --check` also passed.

Authoritative local artifacts:
`.runtime/online-parameters-audit-20261020-jk6aPcXe/verified-regressions.xml`,
`verified-browser-report.json`, and eight `verified-parameters-fixture*.png`
screenshots in that directory. The browser report records no page errors and
identifies the screenshots as isolated fixtures. `verification.json` in the
same directory maps acceptance gates, exact command and tested-source hashes.

The focused regression fixtures were brought forward to the existing process
configuration names, manual-restart contract, nullable diagnostic observations
and retained invalid-cycle audit rows. No optimizer tolerance or control
performance requirement was relaxed to make the suite pass.

| Acceptance gate | Isolated result |
| --- | --- |
| Closed 91-field policies, startup locks, read-only derived values | PASS |
| Next prepared tick, continuous periods, no duplicate numerical calls | PASS |
| Latest kinetics, five-filter refresh, preserved process/history/seeds | PASS |
| Common queue/revision, retries, conflicts, no-op and metadata-only handling | PASS |
| Frame boundary, image warmup, version/interval isolation, marks and counts | PASS |
| Lost messages, checkpoint faults, partial commits and manual restart | PASS |
| Real component HTTP flow and immutable 91-field startup retry | PASS |
| Browser save/dirty/pending/applied states and next-run draft isolation | PASS |
| Parameter/measurement/event CSV and existing annotation serialization | PASS |
| Real RabbitMQ/network outage, InfluxDB Flux round trip | NOT RUN |
| Long Prosys process acceptance, actual camera/GPU and hardware | NOT RUN |

These results establish the implemented online-update protocol and its isolated
regressions; they are not a claim of improved process tracking or a completed
new physical experiment. No running services were redeployed by this change.

The optional Chromium fixture `tests/central_online_parameters_browser.cjs`
renders the actual Central HTML/CSS/JS while intercepting every HTTP request;
it opens no listening server. Its screenshots are **isolated UI fixtures**, not
Prosys experiment results. Set `CENTRAL_PARAMETER_FIXTURE_SCREENSHOT` and
`CENTRAL_PARAMETER_FIXTURE_REPORT` to new output paths when running the browser
test to retain screenshots and the structured execution report.

`tests/test_online_parameter_http_flow.py` additionally drives the actual HTTP
handlers, Central publisher/journal, Controller service and algorithm, and
GSensor service over the explicitly pumped memory bus. No Applied status is
injected: Central observes real application receipts and GSensor acknowledgments.
It also covers an ordinary runtime-control request after a panel update, immutable
startup/draft data, and HTTP End followed by next-experiment draft recovery.
Only optimizer outputs are deterministic fixtures; no listening server,
background service, simulator, camera, or hardware is started.
