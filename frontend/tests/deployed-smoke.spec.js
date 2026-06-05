// End-to-end smoke against the deployed CloudFront URL. Signs in via the
// custom Cloudscape form, then walks every tab and screenshots each.
//
// Pass DASH_URL + TEST_EMAIL + TEST_PASS as env to override defaults.
//
//   DASH_URL=https://... TEST_EMAIL=... TEST_PASS=... \
//     npx playwright test tests/deployed-smoke.spec.js --headed --project=chromium

import { test, expect } from '@playwright/test';

const DASH  = process.env.DASH_URL   || '';
const EMAIL = process.env.TEST_EMAIL || '';
const PASS  = process.env.TEST_PASS  || '';
if (!DASH || !EMAIL || !PASS) {
  throw new Error(
    'deployed-smoke.spec.js needs DASH_URL, TEST_EMAIL, TEST_PASS env vars. ' +
    'Example: DASH_URL=https://<your>.cloudfront.net TEST_EMAIL=you@yourcompany.com ' +
    'TEST_PASS="$DASHBOARD_PASSWORD" npx playwright test tests/deployed-smoke.spec.js'
  );
}

const TABS = [
  'Overview', 'Quotas', 'Cost', 'Health & errors',
  'Latency', 'Capacity & adoption', 'Model insights',
  'Model lifecycle', 'Ops review', 'Settings',
];

test.describe('deployed dashboard smoke', () => {
  test.use({ baseURL: DASH });

  test('sign in and walk every tab', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
    page.on('pageerror', e => consoleErrors.push(`PAGEERROR: ${e.message}`));

    await page.goto('/', { waitUntil: 'networkidle' });

    // Sign-in form: email + password + sign-in button.
    // Placeholders are "you@example.com" and "Your password" — match by
    // the more reliable Cloudscape Input role/name pattern.
    await expect(page.getByPlaceholder(/example|email|you@/i).first())
      .toBeVisible({ timeout: 15000 });
    await page.getByPlaceholder(/example|email|you@/i).first().fill(EMAIL);
    await page.getByPlaceholder(/password/i).first().fill(PASS);
    await page.getByRole('button', { name: /^sign in$/i }).click();

    // Wait for the AppShell to render. Once signed-in, the SPA replaces the
    // AuthApp with the dashboard; the side-nav has links per tab.
    // Look for distinctive AppShell text — a tab name in side-nav.
    await expect(page.getByText(/^overview$/i).first()).toBeVisible({ timeout: 30000 });

    // Screenshot landing page (Overview).
    await page.screenshot({
      path: 'tests/screenshots/00-landing-overview.png',
      fullPage: true,
    });

    // Walk every tab. Cloudscape side-nav uses <a> with text content matching
    // the tab label.
    for (const label of TABS) {
      const link = page.locator(`nav a:has-text("${label}")`).first();
      const count = await link.count();
      console.log(`tab=${label} link_count=${count}`);
      if (count === 0) continue;

      await link.click();
      await page.waitForLoadState('networkidle', { timeout: 12000 }).catch(() => {});
      // Give the tab body a moment to render charts/tables.
      await page.waitForTimeout(1500);

      const slug = label.replace(/[^a-z0-9]/gi, '-').toLowerCase();
      await page.screenshot({
        path: `tests/screenshots/${slug}.png`,
        fullPage: true,
      });
    }

    // Console-error budget: tolerate a couple of noisy ones (some tabs warn
    // when they have empty data) but flag a flood.
    if (consoleErrors.length > 5) {
      console.log('Console errors:', consoleErrors);
    }
    expect(consoleErrors.length, `console errors:\n${consoleErrors.join('\n')}`)
      .toBeLessThanOrEqual(5);
  });
});
