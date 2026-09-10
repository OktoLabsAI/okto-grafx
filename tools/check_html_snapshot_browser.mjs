// Optional browser acceptance; needs Playwright + Chrome, not Grafx runtime deps.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';

const { chromium } = createRequire(import.meta.url)(process.env.GRAFX_PLAYWRIGHT_MODULE ?? 'playwright');
const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 1200 } });
  const errors = [], remote = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.route(/^https?:/, route => { remote.push(route.request().url()); return route.abort(); });
  await page.goto(pathToFileURL(resolve(process.argv[2])).href);
  assert.equal(await page.locator('h1').textContent(), 'Okto Grafx snapshot');
  assert.equal(await page.locator('svg circle').count(), 5);
  assert.equal(await page.locator('svg > path').count(), 6);
  assert.match(await page.locator('body').textContent(), /truncated: false/);
  assert.equal(await page.locator('script,iframe,object,embed').count(), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(remote, []);
  await page.screenshot({ path: process.argv[3], fullPage: true });
  console.log('HTML viewer browser acceptance PASS (5 nodes, 6 edges, no scripts or remote requests).');
} finally {
  await browser.close();
}
