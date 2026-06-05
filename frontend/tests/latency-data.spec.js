import { test, expect } from '@playwright/test';
test('latency tab populates with real data in dev mode', async ({ page }) => {
  page.on('pageerror', e => console.log('PAGEERR', e.message));
  await page.goto('http://localhost:5173/#/latency');
  await page.waitForLoadState('networkidle', { timeout: 15000 });
  await page.waitForTimeout(2500);
  // The "No latency data" empty state should NOT appear.
  const empty = await page.getByText('No latency data').count();
  console.log('No-latency-data text count:', empty);
  expect(empty).toBe(0);
  // Should see model rows (Claude Opus etc.)
  await expect(page.getByText(/claude/i).first()).toBeVisible({ timeout: 5000 });
  await page.screenshot({ path: '/tmp/latency-after.png', fullPage: false });
});
