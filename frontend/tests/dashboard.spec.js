// End-to-end smoke test: load every tab, assert no console errors.
//
// Pre-req: backend running on :8001 and Vite dev server on :5173 (e.g.
// `./scripts/run-local.sh`). Run from frontend/: `npx playwright test`.

import { test, expect } from '@playwright/test';

const TABS = [
  { id: 'overview',     label: 'Overview' },
  { id: 'quotas',       label: 'Quotas' },
  { id: 'errors',       label: 'Health & Errors' },
  { id: 'latency',      label: 'Latency' },
  { id: 'ops',          label: 'Capacity & Adoption' },
  { id: 'ops-review',   label: 'Ops Review' },
];

// React's "Warning:" console.error is also caught here. Some Cloudscape
// components emit benign warnings (deprecated prop names, etc.) that we
// can ignore. Add patterns here if you find one isn't actually a bug.
const IGNORED_PATTERNS = [
  /React DevTools/,
  /Download the React DevTools/,
  // Cloudscape's <BarChart>/<PieChart> emit i18n strings warnings that aren't fatal.
  /Missing.*i18n/i,
];

function shouldFail(text) {
  for (const re of IGNORED_PATTERNS) if (re.test(text)) return false;
  // React's "Warning:" prefix and any thrown error must be flagged.
  return /^Warning:|Error:|Uncaught|TypeError|ReferenceError|RangeError|SyntaxError/i.test(text);
}

test.describe('dashboard smoke', () => {
  test('every tab renders without console errors', async ({ page }) => {
    const errors = [];
    page.on('console', (msg) => {
      const t = msg.text();
      if (msg.type() === 'error' || (msg.type() === 'warning' && t.startsWith('Warning:'))) {
        if (shouldFail(t)) errors.push(`[${msg.type()}] ${t}`);
      }
    });
    page.on('pageerror', (err) => {
      errors.push(`[pageerror] ${err.message}`);
    });

    await page.goto('/');
    // Anchor on the page heading, not the navigation logo span (which is
    // sometimes CSS-hidden under a truncated container).
    await expect(page.getByRole('heading', { name: 'Bedrock Ops Lens', level: 1 })).toBeVisible();

    // Wait for /summary to land before clicking through tabs. Use a generous
    // timeout — the response may already be cached if the page was preloaded.
    try {
      await page.waitForResponse((r) => r.url().includes('/api/summary') && r.ok(),
        { timeout: 5000 });
    } catch {
      // Fallback: wait for the KPI value to render with real number text.
      await expect(page.locator('text=Total requests').first()).toBeVisible();
    }

    for (const tab of TABS) {
      // Sidebar links live in the navigation slot — match by role + label,
      // scoped to the navigation landmark so we don't hit in-content links.
      await page.locator('nav').getByRole('link', { name: tab.label, exact: true }).first().click();
      // Brief settle so the lazy-mounted view's useApi fires + renders.
      await page.waitForTimeout(1500);
    }

    // Final assertion: no error-level console output was captured.
    if (errors.length) {
      console.log('--- captured errors ---');
      for (const e of errors) console.log(' ', e);
    }
    expect(errors, errors.join('\n')).toHaveLength(0);
  });

  test('Info side panel opens on container info click', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Bedrock Ops Lens', level: 1 })).toBeVisible();
    try {
      await page.waitForResponse((r) => r.url().includes('/api/summary') && r.ok(),
        { timeout: 5000 });
    } catch {
      await expect(page.locator('text=Total requests').first()).toBeVisible();
    }

    // Cloudscape's <Link variant="info"> renders as either a <button> or <a>
    // depending on version — match by accessible name regardless of role.
    const infoLink = page.locator('button:has-text("Info"), a:has-text("Info")').first();
    await infoLink.waitFor({ state: 'visible', timeout: 10_000 });
    await infoLink.click();
    // The right-side help panel should now contain "What it shows"
    await expect(page.getByText('What it shows').first()).toBeVisible({ timeout: 5_000 });
  });

  test('Theme toggle changes Cloudscape body class', async ({ page }) => {
    await page.goto('/');
    // Open the theme menu in the top nav
    await page.getByRole('button', { name: /(Light|Dark) mode/ }).click();
    await page.getByRole('menuitem', { name: 'Dark mode' }).click();
    await expect(page.locator('body.awsui-dark-mode')).toBeVisible({ timeout: 3_000 });
  });
});
