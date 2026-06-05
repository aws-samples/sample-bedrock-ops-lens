// Visual screenshot harness — captures every view in light + dark mode
// to test-results/screenshots/. Used for design QA when text-only assertions
// can't catch layout/spacing/empty-state issues.
//
// Run with:    npx playwright test visual.spec.js
// Inspect:     open test-results/screenshots/*.png
import { test } from '@playwright/test';

const VIEWS = [
  { id: 'overview',  label: 'Overview' },
  { id: 'quotas',    label: 'Quotas' },
  { id: 'cost',      label: 'Cost' },
  { id: 'errors',    label: 'Health & Errors' },
  { id: 'latency',   label: 'Latency' },
  { id: 'ops',       label: 'Capacity & Adoption' },
  { id: 'ops-review',label: 'Ops Review' },
  { id: 'settings',  label: 'Settings' },
];

async function setTheme(page, theme) {
  // TopNavigation theme button has aria-label="Toggle dark mode" (stable
  // string regardless of state) and visible text that flips between
  // "☀ Light" / "🌙 Dark". Click only if the visible text doesn't match
  // the desired theme.
  const btn = page.getByRole('button', { name: 'Toggle dark mode' }).first();
  await btn.waitFor({ state: 'visible', timeout: 10_000 });
  const text = (await btn.textContent()) || '';
  const wantsDark = theme === 'dark';
  const isDark = text.toLowerCase().includes('dark');
  if (wantsDark !== isDark) await btn.click();
}

async function gotoView(page, viewId) {
  await page.evaluate((id) => { window.location.hash = `#/${id}`; }, viewId);
  await page.waitForTimeout(2000); // let charts/tables settle
}

for (const theme of ['light', 'dark']) {
  test.describe(`visual snapshot — ${theme} mode`, () => {
    for (const v of VIEWS) {
      test(`${v.label}`, async ({ page }) => {
        await page.goto('/');
        if (theme === 'dark') await setTheme(page, 'dark');
        await gotoView(page, v.id);
        await page.screenshot({
          path: `test-results/screenshots/${theme}-${v.id}.png`,
          fullPage: true,
        });
      });
    }
  });
}
