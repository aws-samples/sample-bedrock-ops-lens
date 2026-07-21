import { test, expect } from '@playwright/test';
const DASH=process.env.DASH_URL, EMAIL=process.env.TEST_EMAIL, PASS=process.env.TEST_PASS;
test.describe('deployed Workloads mantle slice', () => {
  test.use({ baseURL: DASH }); test.setTimeout(90000);
  test('workload-usage has both runtime + mantle on deployed site', async ({ page }) => {
    await page.goto('/', { waitUntil: 'networkidle' });
    await page.getByPlaceholder(/example|email|you@/i).first().fill(EMAIL);
    await page.getByPlaceholder(/password/i).first().fill(PASS);
    await page.getByRole('button', { name: /^sign in$/i }).click();
    await expect(page.getByText(/^overview$/i).first()).toBeVisible({ timeout: 30000 });
    const data = await page.evaluate(async () => {
      const all = await (await fetch('/api/workload-usage?days=5')).json();
      const mantle = await (await fetch('/api/workload-usage?days=5&endpoint=mantle')).json();
      return { allRows: all.length, mantleRows: mantle.length,
               endpoints: [...new Set(all.flatMap(r => r.endpoints || []))] };
    });
    console.log('DEPLOYED workload-usage:', JSON.stringify(data));
    expect(data.allRows).toBeGreaterThan(0);
    expect(data.mantleRows).toBeGreaterThan(0);
    expect(data.endpoints).toContain('mantle');
    expect(data.endpoints).toContain('runtime');
  });
});
