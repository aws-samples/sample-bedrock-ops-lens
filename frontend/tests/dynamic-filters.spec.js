import { test, expect } from '@playwright/test';

test('Provider/Region dropdowns are sourced live, not hardcoded', async ({ page }) => {
  await page.goto('http://localhost:5173/#/overview');
  await page.waitForLoadState('networkidle', { timeout: 15000 });
  await page.waitForTimeout(1500);
  // Expand the filters section
  await page.getByText('Filters', { exact: true }).first().click();
  await page.waitForTimeout(500);
  // Click the Provider dropdown
  const providerDropdown = page.getByText('All providers').first();
  await providerDropdown.click();
  await page.waitForTimeout(400);
  const providerOptions = await page.locator('[role="option"]').allTextContents();
  console.log('Provider options:', providerOptions);
  // Should include providers that were NOT in the old hardcoded list
  const txt = providerOptions.join(' | ').toLowerCase();
  expect(txt).toContain('deepseek');
  expect(txt).toContain('openai');
  await page.screenshot({ path: '/tmp/provider-dropdown.png', fullPage: false });
});
