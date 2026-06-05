import { test, expect } from '@playwright/test';

test('SPA shows Cloudscape sign-in screen, not Cognito Hosted UI', async ({ page }) => {
  page.on('pageerror', e => console.log('PAGEERR:', e.message));
  await page.goto('http://localhost:5173/');
  await page.waitForLoadState('networkidle', { timeout: 15000 });
  // We should NOT be redirected off-domain to Cognito.
  const url = page.url();
  console.log('URL:', url);
  expect(url).toContain('localhost:5173');
  // We should see our heading.
  await expect(page.getByText('Bedrock Ops Lens').first()).toBeVisible();
  await expect(page.getByText('Sign in', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Need an account?')).toBeVisible();
  await page.screenshot({ path: '/tmp/cs-signin-screen.png', fullPage: false });
});

test('Sign-up flow: Cloudscape form posts to /api/auth/signup, gate rejects @gmail.com', async ({ page }) => {
  page.on('pageerror', e => console.log('PAGEERR:', e.message));
  await page.goto('http://localhost:5173/');
  await page.waitForLoadState('networkidle', { timeout: 15000 });
  // Click "Sign up" to switch screens.
  await page.getByText('Sign up').first().click();
  await page.waitForTimeout(500);
  await expect(page.getByText('Create account').first()).toBeVisible();

  // Fill the form with a non-allowed domain.
  await page.locator('input[type="email"]').fill('someone@gmail.com');
  await page.locator('input[type="password"]').fill('TestPassword123!');
  await page.getByRole('button', { name: /create account/i }).click();
  await page.waitForTimeout(3000);

  // The Pre-Sign-Up Lambda should reject; we display its message in <Alert>.
  const body = (await page.locator('body').innerText()).toLowerCase();
  console.log('--- body after signup attempt ---');
  console.log(body.slice(0, 600));
  await page.screenshot({ path: '/tmp/cs-signup-rejected.png', fullPage: false });
  expect(body).toMatch(/amazon\.com|not permitted|restricted/);
});
