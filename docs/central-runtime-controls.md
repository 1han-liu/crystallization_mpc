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
