import { test, expect } from '@playwright/test';
const goto = async (page, h) => { await page.goto(`/#/${h}`); await page.waitForTimeout(1800); };

test.describe('Mantle selective per-tab (thumb rule)', () => {
  test('Cost has NO endpoint switcher (endpoint-agnostic)', async ({ page }) => {
    await goto(page, 'cost');
    // no runtime/mantle segmented control, no "Endpoint-agnostic" badge
    expect(await page.getByText('bedrock-mantle', { exact: true }).count()).toBe(0);
    expect(await page.getByText(/Endpoint-agnostic|Same numbers on both endpoints/i).count()).toBe(0);
  });

  test('Model Lifecycle has NO endpoint switcher', async ({ page }) => {
    await goto(page, 'lifecycle');
    expect(await page.getByText('bedrock-mantle', { exact: true }).count()).toBe(0);
  });

  test('Ops Insights: mantle slice keeps Throttle, hides CRIS panels', async ({ page }) => {
    await goto(page, 'ops');
    // switch to mantle
    await page.getByText('bedrock-mantle', { exact: true }).first().click();
    await page.waitForTimeout(1800);
    // CRIS/service-tier/caching panels hidden on mantle
    expect(await page.getByText('CRIS vs On-Demand', { exact: true }).count()).toBe(0);
    expect(await page.getByText('Service tier distribution', { exact: true }).count()).toBe(0);
    // throttle panel still present (Mantle-supported)
    await expect(page.getByText(/Throttle rate by account/i).first()).toBeVisible();
  });

  test('Model Insights: mantle slice drops Cache hit column', async ({ page }) => {
    await goto(page, 'insights');
    await page.getByText('bedrock-mantle', { exact: true }).first().click();
    await page.waitForTimeout(1800);
    expect(await page.getByText('Cache hit %', { exact: true }).count()).toBe(0);
  });
});
