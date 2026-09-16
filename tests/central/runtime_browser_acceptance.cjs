// Real browser + real HTTP + real RabbitMQ. Run through the Python orchestrator.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const base = process.env.ACCEPTANCE_URL;
const output = process.env.ACCEPTANCE_OUTPUT;
const report = { status: 'FAIL', steps: [], requests: [], pageErrors: [] };
const input = readline.createInterface({ input: process.stdin });
let reply;
input.on('line', line => { const resolve = reply; reply = null; resolve?.(JSON.parse(line)); });
async function control(action) {
  const promise = new Promise(resolve => { reply = resolve; });
  console.log('CONTROL ' + JSON.stringify({ action }));
  return promise;
}
async function json(endpoint, body) {
  const response = await fetch(base + endpoint, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : undefined);
  if (!response.ok) throw new Error(`${endpoint}: HTTP ${response.status}`);
  return response.json();
}
async function until(fn, description, timeout = 25000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const result = await fn();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('Timeout: ' + description);
}
function pass(name, evidence = {}) { report.steps.push({ name, status: 'PASS', ...evidence }); console.log('PASS: ' + name); }
const status = () => json('/api/system/status');

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  page.on('pageerror', error => report.pageErrors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith('/api/operation/controller/runtime')) {
      report.requests.push(request.postDataJSON());
    }
  });
  async function edit(action, expected) {
    const before = (await status()).controller.runtime_controls.revision;
    await until(() => page.locator('#control-target').isEnabled(), 'runtime controls enabled');
    await action();
    const s = await until(async () => {
      const s = await status();
      return s.controller?.runtime_controls?.revision === before + 1 && s.runtime_request?.status === 'applied' && s;
    }, 'Controller acknowledgment');
    for (const [key, value] of Object.entries(expected)) {
      assert.equal(s.controller.runtime_controls.configuration[key], value);
    }
    await page.waitForFunction(() => document.querySelector('#run-configuration-status').textContent === 'applied' && !document.querySelector('#control-target').disabled);
    return s;
  }
  const select = (selector, value, expected) => edit(() => page.locator(selector).selectOption(value), expected);
  const number = (selector, value, expected) => edit(async () => {
    await page.locator(selector).fill(value); await page.locator(selector).press('Enter');
  }, expected);
  try {
    await page.goto(base);
    await until(() => page.locator('#control-target').isEnabled(), 'real Controller available');
    const initial = await status();
    const startup = await json('/api/run-configuration');
    const params = await json('/api/params');
    report.run_id = initial.controller.current_run_id;
    assert.equal(initial.controller.status, 'running');
    assert.equal(initial.controller.parameters.exp_sim, 'simulation');
    assert.equal(initial.controller.parameters.exp_sim_G, 'simulation');
    for (const selector of ['#run-type', '#controller-mode', '#growth-rate-source']) {
      assert.equal(await page.locator(selector).isDisabled(), true);
    }
    pass('real Controller run and scoped UI controls');
    await select('#control-target', 'G', { control_target: 'G' });
    await number('#runtime-g-set', '4e-8', { G_set: 4e-8 });
    await number('#runtime-sigma-set', '0.04', { sigma_set: 0.04, control_target: 'G' });
    await edit(() => page.locator('[data-adaptive-parameter="k_0"]').click(), { adaptation_mode: 'E_A_and_k_0', adaptation_enabled: false });
    await edit(() => page.locator('[data-adaptive-parameter="n"]').click(), { adaptation_mode: 'all', adaptation_enabled: false });
    await select('#adaptation-enabled', 'true', { adaptation_enabled: true });
    await select('#adaptation-enabled', 'false', { adaptation_enabled: false });
    await select('#control-target', 'sigma', { control_target: 'sigma', sigma_set: 0.04 });
    const applied = await status();
    pass('UI edits delivered and acknowledged through real RabbitMQ', { revision: applied.controller.runtime_controls.revision });
    await page.reload();
    await until(() => page.locator('#control-target').isEnabled(), 'reload runtime configuration');
    assert.equal(await page.locator('#runtime-sigma-set').inputValue(), '0.04');
    assert.equal(await page.locator('[data-adaptive-parameter="n"]').isChecked(), true);
    pass('page reload shows Controller actual values');

    await control('disconnect-publisher');
    const revision = applied.controller.runtime_controls.revision;
    await page.locator('#runtime-g-set').fill('5e-8');
    await page.locator('#runtime-g-set').press('Enter');
    const pending = await until(async () => {
      const s = await status(); return s.runtime_request?.status === 'pending' && s;
    }, 'persistent pending request');
    assert.equal(pending.controller.runtime_controls.revision, revision);
    const eventId = pending.runtime_request.command.event_id;
    await page.waitForFunction(() => document.querySelector('#control-target').disabled);
    await control('restart-central');
    await page.reload();
    await until(() => page.locator('#retry-runtime-update').isVisible(), 'retry after Central restart');
    assert.equal((await status()).runtime_request.command.event_id, eventId);
    await page.locator('#retry-runtime-update').click();
    const retried = await until(async () => {
      const s = await status(); return s.runtime_request?.status === 'applied' && s.controller.runtime_controls.revision === revision + 1 && s;
    }, 'retry delivered');
    assert.equal(retried.runtime_request.command.event_id, eventId);
    assert.equal(retried.controller.runtime_controls.configuration.G_set, 5e-8);
    const duplicate = await json('/api/operation/controller/runtime', pending.runtime_request.command);
    assert.equal(duplicate.requested, false);
    assert.equal((await status()).controller.runtime_controls.revision, revision + 1);
    pass('producer disconnect, persisted pending, Central restart and same-event retry', { event_id: eventId });

    const beforeRestart = (await status()).controller;
    await control('stop-controller');
    await until(() => page.locator('#control-target').isDisabled(), 'offline UI locks');
    pass('Controller offline is not reported as applied availability');
    await control('start-controller');
    const recovered = await until(async () => {
      const s = await status(); return s.controller?.available && s.controller.status === 'running' && s.controller;
    }, 'Controller state recovery');
    assert.equal(recovered.current_run_id, beforeRestart.current_run_id);
    assert.equal(recovered.runtime_controls.revision, beforeRestart.runtime_controls.revision);
    assert.deepEqual(recovered.runtime_controls.configuration, beforeRestart.runtime_controls.configuration);
    assert.ok(recovered.scheduler.tick_count >= beforeRestart.scheduler.tick_count);
    await page.reload();
    await until(() => page.locator('#control-target').isEnabled(), 'recovered UI unlocked');
    assert.equal(await page.locator('#runtime-g-set').inputValue(), '5e-8');
    pass('real Controller process restart preserves run, revision, configuration and tick state');
    const historyUrl = '/api/operation/controller/runtime/history?run_id=' + encodeURIComponent(recovered.current_run_id);
    const history = await json(historyUrl);
    assert.equal(history.error, null);
    assert.equal(history.entries.length, recovered.runtime_controls.revision);
    assert.equal(new Set(history.entries.map(e => e.command.event_id)).size, history.entries.length);
    assert.ok(history.entries.every(e => e.result.applied_at && e.result.effective_tick));
    await page.locator('#runtime-history summary').click();
    await until(async () => (await page.locator('#runtime-history-rows tr').count()) === history.entries.length, 'durable history rendered');
    assert.equal(await page.locator('#runtime-history-older').isVisible(), false);
    pass('durable history survives both services restarting without duplicate retries', { events: history.entries.length });
    if (process.env.ACCEPTANCE_GRAFANA_URL) {
      let expectedEvents = history.entries.length;
      if (process.env.ACCEPTANCE_DB_FAULT_FILE) {
        await control('database-offline');
        await number('#runtime-g-set', '6e-8', { G_set: 6e-8, control_target: 'sigma' });
        expectedEvents++;
        await until(async () => (await json(historyUrl)).entries.some(e => e.grafana_sync === 'failed'), 'real database HTTP failure recorded');
        const ticks = (await status()).controller.scheduler.tick_count;
        await until(async () => (await status()).controller.scheduler.tick_count > ticks, 'control loop continues during database outage', 30000);
        await control('database-online');
        pass('database HTTP 503 is visible as failed event sync while numerical clock continues');
      }
      const synced = await until(async () => {
        const h = await json(historyUrl);
        return h.entries.length === expectedEvents && h.entries.every(e => e.grafana_sync === 'synced') && h;
      }, 'all runtime events exported to real InfluxDB', 30000);
      const annotation = JSON.parse(fs.readFileSync(path.join(__dirname, '../../grafana/runtime-configuration-annotation.json')));
      const query = annotation.target.query.replace('v.defaultBucket', JSON.stringify(process.env.ACCEPTANCE_INFLUX_BUCKET))
        .replaceAll('v.timeRangeStart', 'time(v: "2026-01-01T00:00:00Z")')
        .replaceAll('v.timeRangeStop', 'time(v: "2027-01-01T00:00:00Z")')
        .replace('${run_id:regex}', recovered.current_run_id);
      const response = await fetch(process.env.ACCEPTANCE_GRAFANA_URL + '/api/ds/query', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from: '1767225600000', to: '1798761600000', queries: [{ refId: 'A',
          datasource: { uid: 'crystallization-influxdb', type: 'influxdb' }, query, intervalMs: 1000, maxDataPoints: 1000 }] }),
      });
      assert.equal(response.status, 200);
      const queried = await response.json();
      assert.equal(queried.results.A.error, undefined);
      const frames = queried.results.A.frames;
      const eventIds = frames.flatMap(frame => {
        const i = frame.schema.fields.findIndex(field => field.name === 'event_id');
        return i < 0 ? [] : frame.data.values[i];
      });
      assert.equal(new Set(eventIds).size, synced.entries.length);
      report.grafana_query = queried;
      const grafanaPage = await context.newPage();
      await grafanaPage.goto(process.env.ACCEPTANCE_GRAFANA_URL + '/d/crystallization-mpc-analysis?from=now-15m&to=now&var-run_id=' + recovered.current_run_id);
      await grafanaPage.getByText('Runtime configuration applied', { exact: false }).first().waitFor({ timeout: 30000 });
      await grafanaPage.screenshot({ path: path.join(output, 'grafana-events.png'), fullPage: true });
      pass('real InfluxDB events queried by isolated Grafana and dashboard annotation enabled', { events: eventIds.length });
      await grafanaPage.close();
    }
    const final = await until(async () => {
      const s = await status(); return s.controller.scheduler.tick_count > recovered.scheduler.tick_count && s;
    }, 'new numerical tick after recovery', 30000);
    assert.equal(final.controller.scheduler.tick_error_count, 0);
    const lastOutput = final.controller.last_control_output;
    assert.equal(lastOutput.result.valid, true);
    assert.equal(lastOutput.runtime_revision, final.controller.runtime_controls.revision);
    assert.equal(lastOutput.result.target_set, 0.04);
    assert.equal(lastOutput.runtime_configuration.control_target, 'sigma');
    assert.equal(lastOutput.process_write_attempted, false);
    assert.equal(final.controller.integrations.opcua.enabled, false);
    assert.equal(final.controller.integrations.opcua.read_count, 0);
    assert.equal(final.controller.integrations.opcua.write_count, 0);
    assert.deepEqual((await json('/api/run-configuration')).configuration, startup.configuration);
    const afterParams = await json('/api/params');
    for (const key of ['shared', 'controller', 'gsensor']) assert.deepEqual(afterParams[key], params[key]);
    assert.deepEqual(report.pageErrors, []);
    report.final_controller = final.controller;
    pass('control continues after recovery and startup defaults remain unchanged');
    await page.screenshot({ path: path.join(output, 'runtime-browser.png'), fullPage: true });
    report.status = 'PASS';
  } catch (error) {
    report.error = error.stack;
    console.log('FAIL: ' + error.message);
    try { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }); } catch {}
    process.exitCode = 1;
  } finally {
    fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2) + '\n');
    await browser.close();
    input.close();
  }
})().catch(error => { console.error(error.message); process.exit(1); });
