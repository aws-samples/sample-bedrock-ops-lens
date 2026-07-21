import { test, expect } from '@playwright/test';

test.describe('Task A: Workloads view', () => {
  test('Workloads nav item appears when proxy data exists', async ({ page }) => {
    await page.goto('/#/overview');
    await page.waitForTimeout(2000);
    await expect(page.getByText('Workloads', { exact: true }).first()).toBeVisible();
  });

  test('Workloads tab renders per-workload data + download button', async ({ page }) => {
    await page.goto('/#/workloads');
    await page.waitForTimeout(2500);
    // KPI + at least one known workload name in the table
    await expect(page.getByText('search-service').first()).toBeVisible();
    // endpoint switcher present
    await expect(page.getByText('bedrock-mantle', { exact: true }).first()).toBeVisible();
    // CSV download button present
    const dl = page.getByRole('button', { name: /download table as csv/i });
    expect(await dl.count()).toBeGreaterThan(0);
  });

  test('switching to bedrock-mantle keeps the view working', async ({ page }) => {
    await page.goto('/#/workloads');
    await page.waitForTimeout(2000);
    await page.getByText('bedrock-mantle', { exact: true }).first().click();
    await page.waitForTimeout(1500);
    await expect(page.getByText('search-service').first()).toBeVisible();
  });
});
