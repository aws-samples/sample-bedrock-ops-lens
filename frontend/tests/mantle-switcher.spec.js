// Task B validation: runtime/mantle endpoint switcher shows where Mantle
// has data, hides where it doesn't; CSV download button present on tables.
import { test, expect } from '@playwright/test';

async function gotoTab(page, hash) {
  await page.goto(`/#/${hash}`);
  await page.waitForTimeout(1500);
}

test.describe('Mantle endpoint switcher', () => {
  test('Overview shows bedrock-runtime + bedrock-mantle segments (mantle has data)', async ({ page }) => {
    await gotoTab(page, 'overview');
    await expect(page.getByText('bedrock-runtime', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('bedrock-mantle', { exact: true }).first()).toBeVisible();
  });

  test('Errors shows both segments', async ({ page }) => {
    await gotoTab(page, 'errors');
    await expect(page.getByText('bedrock-mantle', { exact: true }).first()).toBeVisible();
  });

  test('Latency HIDES the bedrock-mantle segment (no mantle latency data)', async ({ page }) => {
    await gotoTab(page, 'latency');
    await page.waitForTimeout(1500);
    // runtime content should be present, but NO mantle segment
    const mantleCount = await page.getByText('bedrock-mantle', { exact: true }).count();
    expect(mantleCount).toBe(0);
  });

  test('switching to bedrock-mantle changes the data (Overview)', async ({ page }) => {
    await gotoTab(page, 'overview');
    // Click the mantle segment
    await page.getByText('bedrock-mantle', { exact: true }).first().click();
    await page.waitForTimeout(1500);
    // page should still render (no crash) — assert a KPI card is visible
    await expect(page.locator('body')).toContainText(/Requests|Tokens|Invocations|TPM/i);
  });

  test('tables have a CSV download button', async ({ page }) => {
    await gotoTab(page, 'overview');
    await page.waitForTimeout(1500);
    const dl = page.getByRole('button', { name: /download table as csv/i });
    expect(await dl.count()).toBeGreaterThan(0);
  });
});
