// Run through tests/test_inference_profiles_browser.py: real API + isolated DB.
import { test, expect } from '@playwright/test';

const baseURL = process.env.LENS_AIP_BASE_URL;
const MODEL = 'anthropic.claude-sonnet-4-5-20250929-v1:0';
test.skip(!baseURL, 'Requires the disposable profile test harness');
test.use({ baseURL, trace: 'on', timezoneId: 'UTC' });
test.setTimeout(90_000);

test('profile rollups, history and quota uncertainty reach the UI', async ({ page, request }) => {
  expect(new URL(baseURL).hostname).toBe('127.0.0.1');
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', r => {
    if (r.url().startsWith(`${baseURL}/api/`) && r.status() >= 400)
      errors.push(`${r.status()} ${r.url()}`);
  });
  const get = async path => {
    const r = await request.get(`/api/${path}`);
    expect(r.ok(), `${r.status()} ${path}`).toBeTruthy();
    return r.json();
  };
  expect((await get('summary')).total_requests).toBe(105);
  const models = await get('requests-by-model');
  expect(models.find(m => m.modelid === MODEL).total_requests).toBe(60);
  expect(models).toHaveLength(3);
  expect(models.find(m => m.modelid.endsWith('/dddddd123456')).total_requests).toBe(5);
  const unknown = (await get('model-insights')).find(m => m.modelId.endsWith('/dddddd123456'));
  expect(unknown.public_name).toBe('Unresolved profile (dddddd123456)');
  expect(unknown.provider).toBe('unknown');
  expect(unknown.cost_estimate_usd).toBeNull();
  expect((await get('distinct-filters')).providers.sort()).toEqual(['amazon', 'anthropic']);
  expect((await get('summary?provider=anthropic')).total_requests).toBe(60);
  const profiles = await get('inference-profiles');
  expect(profiles.profiles).toHaveLength(2);
  expect(profiles.profiles.every(p => p.modelId === MODEL && p.resolved)).toBeTruthy();
  expect(profiles.profiles.find(p => p.profile_id === 'bbbbbb123456').api_visible).toBe(false);

  await page.addInitScript(() => localStorage.setItem(
    'bedrock-lens-optional-tabs', JSON.stringify({ workloads: true })));
  await page.goto('/');
  await expect(page.locator('nav').getByRole('link', { name: 'Overview', exact: true }).first()).toBeVisible();
  await expect(page.getByText('Total requests', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('105', { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('overview.png'), fullPage: true });

  const navigate = async label => {
    await page.locator('nav').getByRole('link', { name: label, exact: true }).first().click();
  };
  await navigate('Model Insights');
  await expect(page.getByRole('heading', { name: 'All models (3)', exact: true })).toBeVisible();
  const modelRow = page.getByRole('row').filter({ hasText: MODEL }).first();
  await expect(modelRow.getByRole('cell', { name: '60', exact: true })).toBeVisible();
  await expect(page.getByText('Unresolved profile (dddddd123456)', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('abcdef123456', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('models.png'), fullPage: true });

  const peak = (await get('ops-peak-rpm')).find(r => r.modelId === MODEL);
  expect(peak.has_application_profile).toBe(true);
  expect(peak.peak_requests_hour).toBe(60);
  const dd = await get(`quota-drilldown?account_id=111111111111&region=us-east-1&model_id=${encodeURIComponent(MODEL)}`);
  expect(dd.kpis.peak_rpm).toBe(1);
  expect(dd.kpis.peak_tpm).toBe(100);
  expect(dd.tpm_limit).toBeNull();
  expect(dd.quota_tpm.quota_routing_unknown).toBe(true);
  const direct = await get('quota-drilldown?account_id=111111111111&region=us-east-1&model_id=amazon.nova-pro-v1%3A0');
  expect(direct.tpm_limit).toBe(10000);
  expect(direct.quota_tpm.quota_routing_unknown).toBe(false);
  await navigate('Quotas');
  await expect(page.getByText(/Application profile usage is included/)).toBeVisible();
  await expect(page.getByText('unknown — profile routing unavailable', { exact: true }).first()).toBeVisible();
  const capacity = page.getByRole('table').filter({ hasText: 'TPM limit' })
    .getByRole('row').filter({ hasText: 'Profile test account' });
  await expect(capacity.getByRole('cell').nth(2)).toHaveText('100');
  // Account aggregation must not borrow Nova's known quota for profile traffic.
  for (const index of [3, 4, 6, 7])
    await expect(capacity.getByRole('cell').nth(index)).toHaveText('—');
  await page.screenshot({ path: test.info().outputPath('quotas.png'), fullPage: true });

  await navigate('Model Lifecycle');
  const legacy = page.getByRole('row').filter({ hasText: MODEL }).first();
  await expect(legacy.getByRole('cell', { name: '60', exact: true })).toBeVisible();
  await navigate('Latency');
  await expect(page.getByRole('row').filter({ hasText: MODEL }).first()).toBeVisible();
  await navigate('Usage · Custom Attributes');
  await expect(page.getByText(/Attributed from Bedrock invocation-log tags/)).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'Support' }).first()
    .getByRole('cell', { name: '20', exact: true })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'Analytics' }).first()
    .getByRole('cell', { name: '30', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
