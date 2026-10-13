const { test, expect } = require('@playwright/test');

test('homepage loads and has correct title', async ({ page }) => {
  await page.goto('http://localhost:8080');
  await expect(page).toHaveTitle('Inetometr. Скорость интернета');
});

test('admin page loads with token', async ({ page }) => {
  await page.setExtraHTTPHeaders({ 'Authorization': 'Bearer test_admin_token_123' });
  await page.goto('http://localhost:8080/admin');
  await expect(page).toHaveTitle('Inetometr. Панель');
});