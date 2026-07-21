import { test, expect } from '@playwright/test';
const DASH  = process.env.DASH_URL;
const EMAIL = process.env.TEST_EMAIL;
const PASS  = process.env.TEST_PASS;

test.describe('deployed A+B validation', () => {
  test.use({ baseURL: DASH });
  test.setTimeout(90000);

  test('login + verify A+B behavior against real deployed data', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
    await page.goto('/', { waitUntil: 'networkidle' });
    await expect(page.getByPlaceholder(/example|email|you@/i).first()).toBeVisible({ timeout: 20000 });
    await page.getByPlaceholder(/example|email|you@/i).first().fill(EMAIL);
    await page.getByPlaceholder(/password/i).first().fill(PASS);
    await page.getByRole('button', { name: /^sign in$/i }).click();
    await expect(page.getByText(/^overview$/i).first()).toBeVisible({ timeout: 30000 });

    // Read the real availability signal the UI uses.
    const avail = await page.evaluate(async () => {
      const r = await fetch('/api/distinct-filters'); return (await r.json()).mantle_available;
    });
    console.log('DEPLOYED mantle_available =', JSON.stringify(avail));

    // Task B: Overview switcher visibility must MATCH the data signal.
    await page.goto('/#/overview'); await page.waitForTimeout(2500);
    const mantleOnOverview = await page.getByText('bedrock-mantle', { exact: true }).count();
    if (avail?.volumetric) expect(mantleOnOverview).toBeGreaterThan(0);
    else expect(mantleOnOverview).toBe(0);   // correctly hidden when no data

    // Latency: mantle latency is false in this account → must be hidden.
    await page.goto('/#/latency'); await page.waitForTimeout(2500);
    expect(await page.getByText('bedrock-mantle', { exact: true }).count()).toBe(0);

    // Task A: Workloads view has proxy data → nav + table render.
    await page.goto('/#/workloads'); await page.waitForTimeout(3000);
    await expect(page.getByText('search-service').first()).toBeVisible({ timeout: 15000 });

    // Task 127: CSV download button present.
    expect(await page.getByRole('button', { name: /download table as csv/i }).count()).toBeGreaterThan(0);

    await page.screenshot({ path: 'tests/screenshots/deployed-workloads.png', fullPage: true });
    expect(errors, 'no page errors').toEqual([]);
  });
});
