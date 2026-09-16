// Actual browser/DOM events using production GSensor HTML/JS. Network and
// initialization state are fixtures; no image model, service or device is run.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../../src/crystallization_mpc/apps/gsensor/ui/static');

test('dedicated selector honors capabilities, keeps drafts and submits confirmed method', async t => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome', headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();
  const errors = [], posts = [];
  page.on('pageerror', error => errors.push(error.message));
  // No background polling may overwrite the explicitly controlled fixture.
  await page.addInitScript(() => { window.setInterval = () => 0; });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    const filename = url.pathname === '/' ? 'index.html' : url.pathname.startsWith('/static/') ? path.basename(url.pathname) : null;
    if (filename && fs.existsSync(path.join(root, filename))) return route.fulfill({
      contentType: filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html',
      body: fs.readFileSync(path.join(root, filename)),
    });
    if (route.request().method() === 'POST') {
      posts.push({ path: url.pathname, body: route.request().postDataJSON() });
      return route.fulfill({ json: { session_id: 'session-test', status: 'ready_for_3d', selected_3d_choice: 'a' } });
    }
    return route.fulfill({ status: 503, json: { detail: 'Fixture starts offline' } });
  });
  await page.goto('http://gsensor.test/');
  await page.waitForLoadState('networkidle');
  const select = page.locator('#alignment-method-select');
  assert.equal(await select.isDisabled(), true);
  await page.evaluate(() => {
    Object.assign(state, {
      runId: 'run-test', statusAvailable: true, experimentLifecycleStatus: 'initializing',
      initialized: false, initializationAction: null, dscgrInFlight: false,
      alignmentCapabilitiesReady: true,
      alignmentConfiguration: { method: 'none', confirmed: false, can_select: true },
      alignmentCapabilities: Object.fromEntries(['none', 'centroid', 'fft', 'kalman', 'loftr', 'sift', 'optical_flow', 'ecc']
        .map(method => [method, { method, label: method, available: method !== 'loftr', reason: method === 'loftr' ? 'test checkpoint missing' : '' }])),
    });
    renderInitialization({ session_id: 'session-test', status: 'ready_for_3d', selected_3d_choice: 'a',
      candidates_3d: [{ choice: 'a', label: 'Test candidate' }] });
  });
  assert.equal(await select.isEnabled(), true);
  assert.equal(await select.locator('option').count(), 8);
  assert.equal(await select.locator('option[value="loftr"]').isDisabled(), true);
  assert.equal(await select.locator('option[value="ecc"]').isEnabled(), true);
  await select.selectOption('ecc');
  assert.equal(await page.evaluate(() => state.alignmentDraft), 'ecc');
  assert.equal(posts.length, 0); // Changing method is a draft, not an immediate mutation.
  await page.evaluate(() => renderInitialization(state.initialization));
  assert.equal(await select.inputValue(), 'ecc');
  await page.getByRole('button', { name: 'Confirm selected candidate', exact: true }).click();
  await page.waitForFunction(() => state.confirmedSessionId === 'session-test');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, '/api/initialization/confirm');
  assert.deepEqual(posts[0].body, { session_id: 'session-test', alignment_method: 'ecc' });
  await page.evaluate(() => {
    state.statusAvailable = true;
    state.alignmentConfiguration = { method: 'ecc', confirmed: true, can_select: false };
    renderInitialization(state.initialization);
  });
  assert.equal(await select.inputValue(), 'ecc');
  assert.equal(await select.isDisabled(), true);
  assert.deepEqual(errors, []);
});
