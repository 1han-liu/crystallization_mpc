// Real Chromium UI tests with a deliberately mocked HTTP transport.
// These are not the separate RabbitMQ/Controller end-to-end acceptance.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "../..");
const staticRoot = path.join(root, "src/crystallization_mpc/apps/central/ui/static");

async function fixture(t, { running = true, confirm = true } = {}) {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/usr/bin/google-chrome", headless: true });
  t.after(() => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(6000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const startup = { run_type: "simulation", controller_mode: "MPC", control_target: "sigma",
    adaptation_enabled: false, adaptation_mode: "E_A", growth_rate_source: "simulated" };
  const run = { run_id: "exp-ui-test", status: running ? "starting" : "created",
    created_at: "2026-09-10T12:00:00Z", label: "UI fixture", image_directory: "images" };
  const configuration = { control_target: "sigma", sigma_set: 0.12, G_set: 3e-8,
    adaptation_enabled: false, adaptation_mode: "E_A" };
  const controller = { available: true, current_run_id: run.run_id, status: "running",
    runtime_controls: { supported: true, revision: 0, configuration, recent_events: [] },
    adaptation: { enabled: false, mode: "E_A", fitting: {
      last_status: "not_run", sample_count: 0, minimum_samples: 30, fit_count: 0, failure_count: 0,
    } } };
  let request = null;
  let params = { version: 1, shared: { dt: 5 }, gsensor: { dt_G: 15 },
    controller: { sigma_set: 0.12, G_set: 3e-8 }, meta: {} };
  const posts = [];
  const history = [];
  const f = { page, context, controller, run, startup, posts, errors, confirm };
  f.ack = () => {
    if (!request) return;
    const changes = request.command.changes;
    const before = { ...configuration };
    Object.assign(configuration, changes);
    controller.runtime_controls.revision++;
    controller.adaptation.enabled = configuration.adaptation_enabled;
    controller.adaptation.mode = configuration.adaptation_mode;
    request = { ...request, status: "applied", result: {
      status: "applied", accepted: true, revision: controller.runtime_controls.revision,
      event_id: request.command.event_id, run_id: run.run_id, effective_tick: 2, configuration: { ...configuration },
      before, requested_at: request.command.requested_at, applied_at: "2026-09-10T12:01:00Z",
    } };
    controller.runtime_controls.last_result = request.result;
    controller.runtime_controls.recent_events.push(request.result);
    history.unshift(structuredClone(request));
  };
  f.request = () => request;
  f.params = () => params;
  f.setRequest = value => { request = value; };
  await context.route("http://127.0.0.1:18799/**", async route => {
    const req = route.request();
    const url = new URL(req.url());
    let body;
    if (req.method() !== "GET") {
      body = req.postDataJSON();
      posts.push({ path: url.pathname, body });
    }
    let data = {};
    switch (url.pathname) {
      case "/": return route.fulfill({ contentType: "text/html", body: fs.readFileSync(path.join(staticRoot, "index.html"), "utf8") });
      case "/static/app.js": return route.fulfill({ contentType: "application/javascript", body: fs.readFileSync(path.join(staticRoot, "app.js"), "utf8") });
      case "/static/styles.css": return route.fulfill({ contentType: "text/css", body: fs.readFileSync(path.join(staticRoot, "styles.css"), "utf8") });
      case "/api/ui/config": data = { mode: "development" }; break;
      case "/api/run-configuration":
        if (body) Object.assign(startup, body);
        data = { configuration: startup, defaults: startup }; break;
      case "/api/params":
        if (body) params = { ...params, ...body, version: params.version + 1 };
        data = params; break;
      case "/api/operation/state":
        data = { target: startup.control_target, run_configuration: { configuration: startup }, preview: {} }; break;
      case "/api/system/status":
        data = { experiments: { current_run_id: run.run_id, experiments: [run] },
          current_experiment: run, controller, runtime_request: request, runtime_history: history,
          runtime_history_error: f.historyError || null, gsensor: {} }; break;
      case "/api/operation/controller/runtime/history":
        data = { entries: history, next_cursor: null, grafana_enabled: true }; break;
      case "/api/operation/controller/runtime":
        if (body.expected_revision !== controller.runtime_controls.revision && body.event_id !== request?.command?.event_id) {
          return route.fulfill({ status: 409, json: { detail: "Runtime revision changed; refresh before editing." } });
        }
        request = { command: body, status: "pending", created_at: Date.now() / 1000, result: null };
        data = { requested: true, runtime_request: structuredClone(request) };
        if (f.confirm) f.ack();
        break;
    }
    await route.fulfill({ json: data });
  });
  await page.goto("http://127.0.0.1:18799/");
  await page.waitForFunction(() => document.querySelector("#system-refresh-status").textContent === "online");
  t.after(() => assert.deepEqual(errors, []));
  return f;
}

async function applied(page) {
  await page.waitForFunction(() => document.querySelector("#run-configuration-status").textContent === "applied");
}

test("active configuration with persistence failure is not shown as saved", async t => {
  const f = await fixture(t);
  f.controller.recovery = { status: "persistence_error", error: "disk unavailable" };
  await f.page.locator("#control-target").selectOption("G");
  await f.page.waitForFunction(() => document.querySelector("#run-configuration-status").textContent === "active — not saved");
  assert.match(await f.page.locator("#run-configuration-message").textContent(), /not guaranteed to survive a restart/);
  f.controller.recovery = { status: "saved", error: null };
  await applied(f.page);
});
async function select(f, selector, value) {
  await f.page.locator(selector).selectOption(value);
  await applied(f.page);
}

test("Central history write failure preserves actual settings and shows an explicit warning", async t => {
  const f = await fixture(t);
  await select(f, '#control-target', 'G');
  f.historyError = 'Central runtime persistence incomplete: OSError';
  f.request().persistence_error = f.historyError;
  await f.page.waitForFunction(() => document.querySelector('#run-configuration-status').textContent === 'active — history incomplete');
  assert.equal(await f.page.locator('#control-target').inputValue(), 'G');
  assert.equal(await f.page.locator('#control-target').isEnabled(), true);
  assert.match(await f.page.locator('#feedback-control_target').textContent(), /not durably saved/);
  assert.match(await f.page.locator('#run-configuration-message').textContent(), /Central runtime persistence incomplete/);
  delete f.request().persistence_error;
  f.historyError = null;
  await applied(f.page);
});
async function enter(f, selector, value) {
  await f.page.locator(selector).fill(value);
  await f.page.locator(selector).press("Enter");
  await applied(f.page);
}

test("three scoped controls, checkbox combinations and actual target settings", async t => {
  const f = await fixture(t);
  assert.equal(await f.page.locator("#run-type").isDisabled(), true);
  assert.equal(await f.page.locator("#controller-mode").isDisabled(), true);
  assert.equal(await f.page.locator("#growth-rate-source").isDisabled(), true);
  assert.equal(await f.page.locator("#control-target").isEnabled(), true);
  assert.equal(await f.page.locator("#adaptation-enable-label").textContent(), "Adaptation");
  assert.equal(await f.page.locator("#toggle-adaptation").count(), 0);
  assert.equal(await f.page.locator('[data-adaptive-parameter="k_0"]').isEnabled(), true);
  await f.page.locator('[data-adaptive-parameter="k_0"]').click();
  await applied(f.page);
  assert.equal(f.controller.runtime_controls.configuration.adaptation_mode, "E_A_and_k_0");
  assert.equal(f.controller.adaptation.enabled, false);
  await f.page.locator('[data-adaptive-parameter="n"]').click();
  await applied(f.page);
  assert.equal(f.controller.runtime_controls.configuration.adaptation_mode, "all");
  await select(f, "#adaptation-enabled", "true");
  await select(f, "#control-target", "G");
  await enter(f, "#runtime-g-set", "4e-8");
  assert.equal(f.controller.runtime_controls.configuration.G_set, 4e-8);
  await enter(f, "#runtime-sigma-set", "0.035");
  assert.equal(f.controller.runtime_controls.configuration.control_target, "G");
  await select(f, "#control-target", "sigma");
  assert.equal(await f.page.locator("#runtime-sigma-set").inputValue(), "0.035");
  assert.equal(f.startup.control_target, "sigma");
  assert.equal(f.startup.adaptation_mode, "E_A");
  assert.equal(f.params().controller.G_set, 3e-8);
  assert.equal(await f.page.locator("#retry-runtime-update").isVisible(), false);
  const artifacts = path.join(root, ".runtime/central-runtime-controls/ui");
  fs.mkdirSync(artifacts, { recursive: true });
  await f.page.screenshot({ path: path.join(artifacts, "runtime-controls.png"), fullPage: true });
});

test("pending locks edits, reload retains same event, acknowledgment unlocks", async t => {
  const f = await fixture(t, { confirm: false });
  await f.page.locator("#control-target").selectOption("G");
  await f.page.waitForFunction(() => document.querySelector("#run-configuration-status").textContent === "awaiting confirmation");
  const id = f.posts[0].body.event_id;
  assert.equal(await f.page.locator("#control-target").isDisabled(), true);
  assert.equal(await f.page.locator("#runtime-g-set").isDisabled(), true);
  assert.equal(await f.page.locator("#control-target").inputValue(), "sigma");
  await f.page.reload();
  await f.page.waitForFunction(() => !document.querySelector("#retry-runtime-update").hidden);
  f.confirm = true;
  await f.page.locator("#retry-runtime-update").click();
  await applied(f.page);
  assert.equal(f.posts[1].body.event_id, id);
  assert.equal(await f.page.locator("#control-target").inputValue(), "G");
});

test("last checkbox cannot be removed and invalid values do not send", async t => {
  const f = await fixture(t);
  await f.page.locator('[data-adaptive-parameter="E_A"]').click();
  assert.equal(await f.page.locator('[data-adaptive-parameter="E_A"]').isChecked(), true);
  for (const value of ["", "0", "-1", "NaN", "Infinity", "abc", "0x10", "2e-"]) {
    await f.page.locator("#runtime-g-set").fill(value);
    await f.page.locator("#runtime-g-set").press("Enter");
  }
  assert.equal(f.posts.length, 0);
});

test("Enter plus blur is one submission and stale edited value is rejected", async t => {
  const f = await fixture(t);
  await enter(f, "#runtime-g-set", "4e-8");
  await f.page.locator("#run-configuration-heading").click();
  assert.equal(f.posts.length, 1);
  await f.page.locator("#runtime-g-set").fill("5e-8");
  f.controller.runtime_controls.revision = 2; // Another window changed the configuration.
  f.controller.runtime_controls.configuration.G_set = 6e-8;
  await f.page.waitForTimeout(2200);
  await f.page.locator("#runtime-g-set").press("Enter");
  assert.equal(f.posts.length, 1);
  assert.match(await f.page.locator("#run-configuration-message").textContent(), /changed while you were typing/);
});

test("startup setpoints use parameter save, runtime config uses different path", async t => {
  const f = await fixture(t, { running: false });
  assert.equal(await f.page.locator("#adaptation-enable-label").textContent(), "Adaptation at Start");
  await f.page.locator("#runtime-g-set").fill("4e-8");
  await f.page.locator("#runtime-g-set").press("Enter");
  await f.page.waitForFunction(() => document.querySelector("#runtime-g-set").value === "4e-8" && !document.querySelector("#runtime-g-set").disabled);
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].path, "/api/params");
  assert.equal(f.params().controller.G_set, 4e-8);
});

test("all seven checkbox combinations work while adaptation stays disabled", async t => {
  const f = await fixture(t);
  const modes = { E_A: ["E_A"], k_0: ["k_0"], n: ["n"],
    E_A_and_k_0: ["E_A", "k_0"], E_A_and_n: ["E_A", "n"],
    k_0_and_n: ["k_0", "n"], all: ["E_A", "k_0", "n"] };
  for (const [mode, selected] of Object.entries(modes)) {
    for (const key of selected) {
      const box = f.page.locator(`[data-adaptive-parameter="${key}"]`);
      if (!(await box.isChecked())) { await box.click(); await applied(f.page); }
    }
    for (const key of ["E_A", "k_0", "n"].filter(key => !selected.includes(key))) {
      const box = f.page.locator(`[data-adaptive-parameter="${key}"]`);
      if (await box.isChecked()) { await box.click(); await applied(f.page); }
    }
    assert.equal(f.controller.runtime_controls.configuration.adaptation_mode, mode);
    assert.equal(f.controller.adaptation.enabled, false);
  }
});

test("timeout is visibly unconfirmed, not failed or successfully applied", async t => {
  const f = await fixture(t, { confirm: false });
  await f.page.locator("#control-target").selectOption("G");
  await f.page.waitForFunction(() => document.querySelector("#run-configuration-status").textContent === "awaiting confirmation");
  f.setRequest({ ...f.request(), created_at: Date.now() / 1000 - 31, status: "timeout" });
  await f.page.waitForFunction(() => document.querySelector("#run-configuration-status").textContent === "confirmation timeout");
  assert.equal(await f.page.locator("#retry-runtime-update").isVisible(), true);
  assert.equal(await f.page.locator("#control-target").isDisabled(), true);
  assert.equal(f.controller.runtime_controls.revision, 0);
});

test("offline or wrong-run Controller locks controls without showing startup values as actual", async t => {
  const f = await fixture(t);
  f.controller.available = false;
  await f.page.waitForFunction(() => document.querySelector("#control-target").disabled);
  assert.equal(await f.page.locator("#run-configuration-status").textContent(), "unconfirmed");
  f.controller.available = true;
  f.controller.current_run_id = "other-run";
  await f.page.waitForFunction(() => document.querySelector("#control-target").value === "");
  assert.equal(await f.page.locator("#runtime-g-set").inputValue(), "");
  assert.equal(await f.page.locator("#runtime-g-set").isDisabled(), true);
  assert.equal(f.posts.length, 0);
});

test("stale dropdown update reports rejected even without a Controller event", async t => {
  const f = await fixture(t);
  f.controller.runtime_controls.revision = 1;
  await f.page.locator("#control-target").selectOption("G");
  await f.page.waitForFunction(() => document.querySelector("#run-configuration-status").textContent === "rejected");
  assert.equal(await f.page.locator("#control-target").inputValue(), "sigma");
  assert.match(await f.page.locator("#run-configuration-message").textContent(), /revision changed/);
});

test("target roles and editing conversions retain automatic standby updates", async t => {
  const f = await fixture(t);
  assert.match(await f.page.locator("#role-sigma_set").textContent(), /Current target/);
  assert.match(await f.page.locator("#role-G_set").textContent(), /Standby/);
  await f.page.locator("#runtime-g-set").fill("2e-8");
  assert.equal(await f.page.locator("#conversion-G_set").textContent(), "Editing preview: 0.02 μm/s");
  assert.equal(f.posts.length, 0);
  await f.page.locator("#runtime-g-set").press("Enter");
  await applied(f.page);
  assert.match(await f.page.locator("#feedback-G_set").textContent(), /3e-8 → 2e-8.*Standby value updated/);
  assert.equal(f.controller.runtime_controls.configuration.control_target, "sigma");
  await select(f, "#control-target", "G");
  assert.match(await f.page.locator("#role-sigma_set").textContent(), /Standby/);
  assert.match(await f.page.locator("#role-G_set").textContent(), /Current target/);
  await f.page.reload();
  await f.page.waitForFunction(() => document.querySelector("#feedback-G_set").textContent.includes("2e-8"));
  assert.match(await f.page.locator("#feedback-control_target").textContent(), /Using G_set = 2e-8/);
  await f.page.locator("#runtime-sigma-set").fill("0.035");
  assert.match(await f.page.locator("#conversion-sigma_set").textContent(), /3.5%/);
  for (const value of ["", "2e-", "NaN", "Infinity", "0x10", "-1"]) {
    await f.page.locator("#runtime-sigma-set").fill(value);
    assert.equal(await f.page.locator("#conversion-sigma_set").textContent(), "Enter a finite positive decimal value.");
  }
});

test("inline fitting distinguishes disabled history, missing counts and failures", async t => {
  const f = await fixture(t);
  assert.match(await f.page.locator("#runtime-fitting").textContent(), /Parameter updates stopped/);
  f.controller.adaptation.fitting = { last_status: "fit_succeeded", last_success_at: "2026-09-10T12:01:00Z" };
  await f.page.waitForFunction(() => document.querySelector("#runtime-fitting").textContent.includes("Last fit succeeded"));
  assert.match(await f.page.locator("#runtime-fitting").textContent(), /completed fits unavailable/);
  await select(f, "#adaptation-enabled", "true");
  Object.assign(f.controller.adaptation.fitting, { last_status: "fit_failed", last_failure_reason: "invalid sample", failure_count: 1 });
  await f.page.waitForFunction(() => document.querySelector("#runtime-fitting").textContent.includes("invalid sample"));
  assert.match(await f.page.locator("#runtime-fitting").textContent(), /Last successful fit:/);
});

test("history shows submitted versus applied time", async t => {
  const f = await fixture(t);
  await select(f, "#control-target", "G");
  await enter(f, "#runtime-g-set", "2e-8");
  await f.page.locator("#runtime-history summary").click();
  await f.page.waitForFunction(() => document.querySelectorAll("#runtime-history-rows tr").length === 2);
  assert.match(await f.page.locator("#runtime-history-rows").textContent(), /Applied:.*revision 2/s);
  assert.equal(await f.page.locator("#runtime-history-older").isVisible(), false);
  await f.page.screenshot({ path: path.join(root, ".runtime/central-runtime-controls/ui/configuration-observability.png"), fullPage: true });
});
