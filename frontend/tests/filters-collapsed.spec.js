import { test, expect } from '@playwright/test';

test('filters are collapsed by default with a summary line', async ({ page }) => {
  await page.goto('http://localhost:5173/#/overview');
  await page.waitForLoadState('networkidle', { timeout: 15000 });
  await page.waitForTimeout(2000);

  // Header text "Filters" should be visible
  await expect(page.getByText('Filters', { exact: true }).first()).toBeVisible();

  // Summary line should show defaults — "Last 14 days" + "All accounts"
  await expect(page.getByText(/Last 14 days/).first()).toBeVisible();
  await expect(page.getByText(/All accounts/).first()).toBeVisible();

  // The actual filter inputs (Date range label) should NOT be visible (collapsed)
  const dateRange = page.getByText('Date range', { exact: true }).first();
  expect(await dateRange.isVisible()).toBe(false);

  // Click the chevron / header to expand
  await page.getByText('Filters', { exact: true }).first().click();
  await page.waitForTimeout(500);
  await expect(page.getByText('Date range', { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: '/tmp/filters-expanded.png', fullPage: false });
});
