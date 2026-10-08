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
  expect(unknown.public_name).toBe('Profile dddddd123456 — model not identified');
  expect(unknown.provider).toBe('unknown');
  expect(unknown.cost_estimate_usd).toBeNull();
  expect((await get('distinct-filters')).providers.sort()).toEqual(['amazon', 'anthropic']);
  expect((await get('summary?provider=anthropic')).total_requests).toBe(60);
  const profiles = await get('inference-profiles');
  expect(profiles.profiles).toHaveLength(2);
  expect(profiles.profiles.every(p => p.modelId === MODEL && p.resolved)).toBeTruthy();
  expect(profiles.profiles.find(p => p.profile_id === 'bbbbbb123456').api_visible).toBe(false);
  const usage = await get('inference-profile-usage');
  expect(usage).toHaveLength(3);
  const supportUsage = usage.find(r => r.application_profile_name === 'Support assistant');
  expect(supportUsage.total_requests).toBe(20);
  expect(supportUsage.invoked_identifiers).toHaveLength(2);
  expect(supportUsage.modelid).toBe(MODEL);
  expect(usage.reduce((sum, row) => sum + row.total_requests, 0)).toBe(55);
  expect(usage.find(r => !r.resolved).modelid).toBeNull();

  await page.addInitScript(() => localStorage.setItem(
    'bedrock-lens-optional-tabs', JSON.stringify({ workloads: true })));
  await page.goto('/');
  await expect(page.locator('nav').getByRole('link', { name: 'Overview', exact: true }).first()).toBeVisible();
  await expect(page.getByText('Total requests', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('105', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('heading', { name: /Application inference profiles/ })).toBeVisible();
  const profileTable = page.getByRole('table').filter({ hasText: 'Invoked as' });
  const supportRow = profileTable.getByRole('row').filter({ hasText: 'Support assistant' });
  await expect(supportRow).toHaveCount(1);
  await expect(supportRow).toContainText('ARN: abcdef123456');
  await expect(supportRow).toContainText('short ID: abcdef123456');
  await expect(supportRow).toContainText(MODEL);
  await expect(profileTable.getByText('Name not discovered', { exact: true })).toBeVisible();
  await expect(profileTable.getByText('not identified yet', { exact: true })).toBeVisible();
  await expect(profileTable.getByText('not currently listed', { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('overview.png'), fullPage: true });

  const navigate = async label => {
    await page.locator('nav').getByRole('link', { name: label, exact: true }).first().click();
  };
  await navigate('Model Insights');
  await expect(page.getByRole('heading', { name: 'All models (3)', exact: true })).toBeVisible();
  const modelRow = page.getByRole('row').filter({ hasText: MODEL }).first();
  await expect(modelRow.getByRole('cell', { name: '60', exact: true })).toBeVisible();
  await expect(page.getByText('Profile dddddd123456 — model not identified', { exact: true }).first()).toBeVisible();
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
  // The page-level banners were replaced by per-row indicators.
  await expect(page.getByText(/Application profile usage is included/)).toHaveCount(0);
  await expect(page.getByText(/Minute coverage is incomplete/)).toHaveCount(0);
  await expect(page.getByText('unknown — profile routing unavailable', { exact: true }).first()).toBeVisible();
  // A known limit with a measurement always yields a percentage; without
  // complete minute coverage it is a lower bound, never a blank "Unknown".
  await expect(page.getByText(/^≥ \d+(\.\d+)?%$/).first()).toBeVisible();
  const capacityTable = page.getByRole('table').filter({ hasText: 'TPM limit' });
  const capacity = capacityTable.getByRole('row').filter({ hasText: 'Profile test account' });
  const headers = await capacityTable.getByRole('columnheader').allTextContents();
  const capacityCell = header => {
    const index = headers.findIndex(text => text.includes(header));
    expect(index, `column ${header}`).toBeGreaterThanOrEqual(0);
    return capacity.getByRole('cell').nth(index);
  };
  await expect(capacityCell('Hourly avg TPM/min')).toHaveText('100');
  await expect(capacityCell('Peak est. quota TPM')).toContainText('unavailable');
  // Account aggregation must not borrow Nova's known quota for profile traffic:
  // no limit (labelled, not blank) and no percentage.
  for (const header of ['TPM limit', 'RPM limit'])
    await expect(capacityCell(header)).toHaveText('routing unknown');
  for (const header of ['TPM util %', 'RPM util %'])
    await expect(capacityCell(header)).toHaveText('—');
  // Live pull is opt-in per deployment; this harness has no live-pull function,
  // so the panel says so and offers no button to press.
  const live = page.locator('[data-testid="live-pull"]');
  await expect(live.getByRole('heading', { name: 'Minute-by-minute (live)' })).toBeVisible();
  await expect(live.getByText('Live pull is not configured in this deployment.')).toBeVisible();
  await expect(live.getByRole('button', { name: 'Pull live data' })).toBeDisabled();
  await page.screenshot({ path: test.info().outputPath('quotas.png'), fullPage: true });

  // Prompt caching rows come from the reviewed catalog plus measured cache
  // metrics. These fixture rows have no cache counters, so usage is unknown and
  // nothing is recommended; the unresolved profile is not in the catalog.
  await navigate('Ops Review');
  const cachingSection = page.locator('#ops-section-caching');
  await expect(cachingSection.getByRole('heading', { name: 'Prompt caching' })).toBeVisible();
  const sonnetRow = cachingSection.getByRole('row').filter({ hasText: `${MODEL} (Claude Sonnet 4.5)` });
  await expect(sonnetRow).toContainText('Documented: implicit and explicit');
  await expect(sonnetRow).toContainText('Usage cannot be determined, so no recommendation.');
  await expect(cachingSection.getByText(/Not in the reviewed catalog, so no recommendation: .*dddddd123456/)).toBeVisible();
  await expect(cachingSection.getByRole('link', { name: 'Prompt caching for faster model inference' })).toHaveAttribute(
    'href', 'https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html');
  await expect(page.getByText(/prompt-caching potential/)).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('ops-review.png'), fullPage: true });

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

test('Overview discloses a failed profile-usage request', async ({ page }) => {
  await page.route('**/api/inference-profile-usage*', route => route.fulfill({
    status: 503, contentType: 'application/json', body: '{"detail":"temporarily unavailable"}',
  }));
  await page.goto('/');
  await expect(page.getByText('Application profile usage could not be loaded. Try refreshing the page.'))
    .toBeVisible();
  await expect(page.getByText('Total requests', { exact: true }).first()).toBeVisible();
});

// The live-pull function itself is exercised against real CloudWatch and with
// mocked boto3 in tests/test_live_pull*.py. Here only its HTTP answers are
// stubbed, to pin how the drill-down renders them.
const LIVE_WINDOW = { start: '2026-10-06T11:00:00+00:00', end: '2026-10-06T12:00:00+00:00', hours: 1 };
const liveMinute = (mm, extra) => ({
  minute: `2026-10-06T11:${String(mm).padStart(2, '0')}:00+00:00`, requests: 10,
  input_tokens: 400, output_tokens: 100, quota_tpm: 900, quota_source: 'aws_estimate', ...extra,
});
const liveResult = extra => ({
  ok: true, status: 'complete', cached: false, pulled_at: '2026-10-06T12:00:41+00:00',
  window: LIVE_WINDOW, burndown_rate: 1, burndown_rate_source: 'bundled_default',
  active_minutes: 3, unknown_minutes: { quota_tpm: 1, requests: 0, input_tokens: 1, output_tokens: 1 },
  quota_sources: { aws_estimate: 2, reconstructed: 0, mixed: 0 },
  minutes: [liveMinute(5), liveMinute(30, { requests: 40, quota_tpm: 8000 }),
            liveMinute(31, { quota_tpm: null, input_tokens: null, output_tokens: null })],
  peak: { quota_tpm: { value: 8000, at: '2026-10-06T11:30:00+00:00' },
          rpm: { value: 40, at: '2026-10-06T11:30:00+00:00' },
          input_tokens: { value: 400, at: '2026-10-06T11:05:00+00:00' },
          output_tokens: { value: 100, at: '2026-10-06T11:05:00+00:00' } },
  has_application_profile: false, ...extra,
});

test('live pull charts the pulled minutes against the applied limit', async ({ page }) => {
  const posted = [];
  let reply = route => route.fulfill({ json: liveResult({
    model_id: 'amazon.nova-pro-v1:0', identifiers: ['amazon.nova-pro-v1:0'],
    identifiers_with_data: ['amazon.nova-pro-v1:0'] }) });
  await page.route('**/api/live-pull', route => {
    if (route.request().method() === 'GET')
      return route.fulfill({ json: { enabled: true, hours: [1, 3, 6, 12, 24], cooldown_seconds: 15 } });
    posted.push(route.request().postDataJSON());
    return reply(route);
  });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await page.locator('nav').getByRole('link', { name: 'Quotas', exact: true }).first().click();
  const live = page.locator('[data-testid="live-pull"]');
  await expect(live.getByText(/Press Pull live data/)).toBeVisible();

  // Nova is called directly, so its applied 10K TPM / 1K RPM limits apply.
  await page.getByRole('button', { name: /111111111111 · anthropic/ }).click();
  await page.getByText('111111111111 · amazon.nova-pro-v1:0 · us-east-1', { exact: true }).click();
  await live.getByText('1h', { exact: true }).click();
  await live.getByRole('button', { name: 'Pull live data' }).click();
  await expect(live.getByText('Pulled Oct 6 12:00 UTC · last 1 hour · 3 minutes with data')).toBeVisible();
  expect(posted).toEqual([{ account_id: '111111111111', model_id: 'amazon.nova-pro-v1:0',
    region: 'us-east-1', endpoint: 'runtime', hours: 1 }]);
  const tpm = live.locator('[data-testid="live-tpm"]');
  await expect(tpm.getByText('10.0K').first()).toBeVisible();
  // One active minute could not be established, so the peak is a lower bound.
  await expect(tpm.getByText('≥ 8.0K')).toBeVisible();
  await expect(tpm.getByText('≥ 80.0%')).toBeVisible();
  const rpm = live.locator('[data-testid="live-rpm"]');
  await expect(rpm.getByText('1.0K').first()).toBeVisible();
  await expect(rpm.getByText('4.0%', { exact: true })).toBeVisible();
  await expect(live.getByText(/Quota TPM source: AWS EstimatedTPMQuotaUsage for 2 min/)).toBeVisible();
  await expect(live.getByText(/a peak marked ≥ is a lower bound/)).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('live-pull.png'), fullPage: true });

  // A failed/partial read with no samples cannot claim there was no traffic.
  reply = route => route.fulfill({ json: liveResult({
    model_id: 'amazon.nova-pro-v1:0', identifiers: ['amazon.nova-pro-v1:0'],
    identifiers_with_data: [], status: 'partial', active_minutes: 0,
    minutes: [], peak: {},
  }) });
  await live.getByRole('button', { name: 'Pull live data' }).click();
  await expect(live.getByText('CloudWatch data could not be established for this window. Try again.'))
    .toBeVisible();
  await expect(live.getByText(/No Bedrock requests reported/)).toHaveCount(0);

  // A different selection never shows the previous selection's pull.
  await page.getByRole('button', { name: /amazon\.nova-pro/ }).click();
  await page.getByText(/^111111111111 · anthropic/).click();
  await expect(live.getByText(/Press Pull live data/)).toBeVisible();

  // Profile traffic in the pulled window: no limit is borrowed for it.
  reply = route => route.fulfill({ json: liveResult({
    model_id: 'anthropic.claude-sonnet-4-5-20250929-v1:0', burndown_rate: 5,
    has_application_profile: true,
    identifiers: ['anthropic.claude-sonnet-4-5-20250929-v1:0', 'arn:aws:bedrock:us-east-1:111111111111:application-inference-profile/abcdef123456', 'abcdef123456'],
    identifiers_with_data: ['anthropic.claude-sonnet-4-5-20250929-v1:0', 'abcdef123456'],
    unknown_minutes: { quota_tpm: 0, requests: 0, input_tokens: 0, output_tokens: 0 } }) });
  await live.getByRole('button', { name: 'Pull live data' }).click();
  await expect(live.getByRole('heading', { name: 'Quota TPM per minute — 5× output burndown' })).toBeVisible();
  await expect(live.getByText('unknown — profile routing unavailable').first()).toBeVisible();
  await expect(live.getByText(/Combines direct calls with 2 application inference profile identifiers/)).toBeVisible();
  await expect(live.getByText('≥ 8.0K')).toHaveCount(0);

  // A refused pull shows the server's reason.
  reply = route => route.fulfill({ status: 403, json: {
    detail: 'Lens cannot read CloudWatch in account 111111111111 (us-east-1).' } });
  await live.getByRole('button', { name: 'Pull live data' }).click();
  await expect(live.getByText('Lens cannot read CloudWatch in account 111111111111 (us-east-1).')).toBeVisible();

  // An intermediary's HTML page with a 200 (CloudFront serves index.html for any
  // 404) must read as an error, never as an empty pull.
  reply = route => route.fulfill({ status: 200, contentType: 'text/html',
    body: '<!DOCTYPE html><html><body><div id="root"></div></body></html>' });
  await live.getByRole('button', { name: 'Pull live data' }).click();
  await expect(live.getByText('Unexpected response from the live-pull endpoint.')).toBeVisible();
  expect(errors).toEqual([]);
});

test('switching the quota endpoint changes the hourly request and disables Mantle live pull', async ({ page }) => {
  const endpoints = [];
  await page.route('**/api/distinct-filters*', async route => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ json: {
      ...data, mantle_available: { ...data.mantle_available, volumetric: true },
    } });
  });
  await page.route('**/api/ops-peak-rpm*', route => route.fulfill({ json: [] }));
  await page.route('**/api/quota-drilldown/options*', route => {
    const endpoint = new URL(route.request().url()).searchParams.get('endpoint');
    return route.fulfill({ json: { options: [{
      accountId: '111111111111', modelId: MODEL, region: 'us-east-1', endpoint,
      label: `111111111111 · ${MODEL} · us-east-1`, total_requests: 100,
    }] } });
  });
  await page.route(/\/api\/quota-drilldown(?:\?|$)/, route => {
    endpoints.push(new URL(route.request().url()).searchParams.get('endpoint'));
    return route.fulfill({ json: { series: [], kpis: {} } });
  });
  await page.route('**/api/live-pull', route => route.fulfill({ json: { enabled: true } }));
  await page.goto('/#/quotas');
  await expect.poll(() => endpoints.at(-1)).toBe('runtime');
  await page.getByText('bedrock-mantle', { exact: true }).click();
  await expect.poll(() => endpoints.at(-1)).toBe('mantle');
  const live = page.locator('[data-testid="live-pull"]');
  await expect(live.getByText('Live pull covers the bedrock-runtime endpoint only.')).toBeVisible();
  await expect(live.getByRole('button', { name: 'Pull live data' })).toBeDisabled();
});
