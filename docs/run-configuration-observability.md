# Run Configuration observability

> Distribution note: test suites and acceptance/demo scripts are local-only
> and excluded from Git. Verification commands below require those local files;
> they are not available in a fresh clone. Runtime telemetry and Grafana
> configuration tools remain part of the repository.

## Contract

Run setup (Run Type, Controller Mode, Growth-rate Source) is locked after start.
Runtime controls retain automatic submission, one selected control target, and
two editable setpoints. Enter/blur commits numeric edits once. Current/standby
labels use the Controller-confirmed target, not an unconfirmed dropdown value.
Conversions are display-only: sigma × 100 percent and G × 1e6 micrometres/second.
The backend still receives dimensionless sigma and G in metres/second.

Each field displays its latest request outcome and actual before/after values.
Pending and timeout are unconfirmed, not rejected or applied. Persistence failure
is shown separately from application. A failed history write is not hidden.
The page preserves existing same-event retry and revision conflict protection.

Fitting diagnostics next to Adaptation show actual sample/fit/failure counts,
selected parameters, last successful fit time and last failure reason. Missing
values say unavailable. Disabled adaptation retains learned parameters and shows
historical fitting results, not a currently running fit. The successful-fit wall
clock is service metadata, recorded only when the real fit counter increases and
restored with the service's version-1 state. It is not part of the deterministic
algorithm state; neither fitting formulas nor numerical strategy were changed.

## Durable history and APIs

- Controller: `GET /api/runtime-history?run_id=...&after=0&limit=100` for ascending
  synchronization, or `before=<cursor>` for descending pages. Limit 1–100.
- Central: `GET /api/operation/controller/runtime/history?run_id=...&before=...&limit=30`.
  This validates the experiment and reconciles available Controller history.
- Existing runtime command and last-request interfaces are unchanged. System
  status additionally supplies the latest request per edited field, even when
  that edit is older than the last 50 events.

Version-1 JSON journals `.controller_runtime_history.json` and
`.central_runtime_history.json` live under each service's experiment root. Each
service owns its file (single Central process). Atomic replacement and fsync
protect persisted entries. `(run_id,event_id)` identifies an operation; retry
updates one record. A new event cannot reuse that identity with different data.
Pending, timeout and rejected submissions are retained. Central-side revision
rejections are labelled as Central decisions, never Controller applications.

Controller outcome history survives the 50-entry status window and process
restart. Its existing current-run state is a recovery source after journal write
failure. Central reconciles paginated outcomes in a background worker, including
when no browser is open. Legacy latest-request/current-run records are imported
where available. No missing historical events or timestamps are fabricated.

## Grafana events

The independent `controller_runtime_event` measurement contains actual before/
after settings, revision, effective tick and submission/application timestamps.
Only changed, applied configurations are annotated. The point timestamp is
Controller `applied_at`; `effective_tick` identifies the scheduled next cycle,
not proof that the cycle already executed. Old records without exact application
time are explicitly unavailable for annotation. Rejected/timeout/no-change
requests remain in history but do not create successful-change markers.

Central exports from its durable journal outside the control loop. Identical
event tags and exact timestamp make retries idempotent. Network failures retain
pending/failed state for retry, and the UI labels Grafana synchronization as
incomplete. An InfluxDB outage does not stop the Controller algorithm clock.

`CENTRAL_RUNTIME_INFLUX_ENABLED` defaults to `CONTROLLER_INFLUX_ENABLED`. It uses
the existing `CONTROLLER_INFLUX_URL/TOKEN/ORG/BUCKET` settings. Disabled or missing
configuration is not displayed as successful synchronization. Never put tokens
in Git or browser responses. The normal fresh-simulation launcher already passes
the whitelisted local telemetry settings to both services.

The annotation template is `grafana/runtime-configuration-annotation.json`.
It is deliberately outside the live provisioning mount: implementation must not
hot-change a user's ongoing demonstration. To generate a dashboard for an
isolated provisioning folder, from the project root run:

```bash
.venv/bin/python scripts/build_runtime_dashboard.py --output /absolute/test/provisioning/crystallization-mpc.json
```

During an agreed rollout window, the same builder can target the existing
`grafana/dashboards/crystallization-mpc.json`; it replaces the named annotation
idempotently and retains existing panels. Reload Central/Controller code only
after safely ending the active experiment. Do not run the fresh-session cleanup
script merely to refresh this UI. Existing Grafana data does not need deletion.

The query follows the official [InfluxDB annotation interface](https://grafana.com/docs/grafana/latest/datasources/influxdb/annotations/)
and returns time/text fields filtered by experiment and dashboard time range.

## Verification and boundaries

Baseline before these changes: **301 passed / 13 failed** (the known MATLAB
parameter-fit numerical tolerance tests). The first broad post-change run caught
a wall-clock value in algorithm recovery state; this was fixed by moving it to
service metadata rather than weakening deterministic comparison tests.

Verified evidence (2026-09-10):

- 13 Chromium interaction tests: roles, conversions, seven checkbox combinations,
  autosubmit, pending/retry, stale revisions, missing/failed fitting diagnostics,
  reload feedback and history rendering.
- Real Controller test: 65 changes retained across restart, beyond the 50-event
  status window; repeated event remains a single entry. Journal tests cover 125
  entries, pagination, run isolation, unknown legacy time and idempotent export.
- Final broad regression: **309 passed / same 13 failed**. Two subsequent
  targeted additions also passed: 125-event Controller-to-Central page
  reconciliation/deduplication, and visible journal disk failure followed by
  recovery from persisted Controller state without stopping the algorithm clock.
- Actual browser → Central → RabbitMQ → Controller → history → InfluxDB → Grafana:
  `.runtime/run-configuration-acceptance/56c8387eec9542cc95fc39dc255d4c3e/platform/report.json`.
  This includes message publisher failure, service restarts, database HTTP 503,
  control-clock continuation and event catch-up. Grafana query results and a
  rendered annotation screenshot are retained alongside the report.

Reusable tests:

```bash
.venv/bin/python -m pytest tests/central tests/controller -q
NODE_PATH=/home/laniakea/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node --test tests/central/runtime_ui.test.cjs
.venv/bin/python scripts/verify_runtime_observability.py
```

The full-chain verifier requires the existing local RabbitMQ and InfluxDB, local
private test-provisioning credentials, Chromium/Playwright and the pinned
Grafana image. It creates a unique bucket/token and ephemeral Grafana instance,
then deletes only those test resources. Test measurements are not retained in
InfluxDB; reports/screenshots remain on disk. User services/experiments/images
are not stopped or deleted. Generated credential files are private and their
test token is revoked during cleanup.

This verifies operator configuration and observability, not closed-loop control
performance, physical safety or real crystallization. Existing MATLAB tolerance
failures and default-input adaptation-domain failures are not fixed or hidden.
Live rollout and Git commit/push are separate operator-authorized steps.

## Pre-submission fixes (2026-09-10)

- Central status now preserves the real Controller response when its request or
  history journal cannot be read/written. Errors are reported separately, and
  confirmed-but-not-persisted requests remain visibly applied with a storage
  warning. Corrupt journals are not silently overwritten. Submission still
  requires successful persistence before sending a new command.
- Request observation also looks up matching `(run_id,event_id,command)` in the
  full durable history, after Controller-history synchronization. A confirmed
  outcome outside the recent 50-event window no longer remains falsely timed out.
- Fresh-clone startup accepts `--rabbit-env`, `RABBIT_URL`, or the ignored
  `config/rabbitmq.env`, while preserving the previous local fallback. The empty
  template and [setup instructions](fresh-simulation-setup.md) contain no private
  credentials. Cleanup scope is unchanged and explicitly documented.
- GSensor's obsolete shared-parameter-editor assertion was replaced with a
  dedicated-selector contract test plus actual Chromium DOM interaction tests:
  capability availability, draft retention, confirm payload and post-confirm lock.

Validation: full Python suite **393 passed / the same 13 MATLAB tolerance
failures**; four subsequently added corrupt-journal cases also passed (25/25
Central runtime HTTP tests). Central + GSensor browser suite **15 passed**.
Real isolated RabbitMQ/Controller/Central restart-and-retry acceptance **PASS**:
`.runtime/central-runtime-controls/browser-20260910T171624981756Z/report.json`.
No user experiment restart, numerical-strategy change, Git commit or push was
performed. Grafana/InfluxDB full-chain acceptance was not rerun for these fixes;
the earlier evidence above remains separate from this narrower verification.
