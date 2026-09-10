import { expect, test } from "@playwright/test";

const API_URL = "http://127.0.0.1:8000";
const WEB_URL = "http://127.0.0.1:3000";
const ADMIN_URL = "http://127.0.0.1:3001";

test.describe.serial("Plane OIDC SSO", () => {
  test("signs in through authorization code + PKCE and logs out locally", async ({ page }) => {
    await page.goto(WEB_URL);
    const ssoButton = page.getByText(/with Local OIDC/i).first();
    await expect(ssoButton).toBeVisible();
    await ssoButton.click();

    await page.waitForURL((url) => url.origin === WEB_URL && !url.searchParams.has("error_code"));
    const me = await page.request.get(`${API_URL}/api/users/me/`);
    expect(me.ok()).toBeTruthy();
    expect((await me.json()).email).toBe("sso.e2e@example.com");

    const csrf = await page.request.get(`${API_URL}/auth/get-csrf-token/`);
    const { csrf_token: csrfToken } = await csrf.json();
    const signout = await page.request.post(`${API_URL}/auth/sign-out/`, {
      headers: { "X-CSRFToken": csrfToken, Origin: WEB_URL, Referer: `${WEB_URL}/` },
      maxRedirects: 0,
    });
    expect(signout.status()).toBe(302);
    expect(signout.headers().location).toContain("https://127.0.0.1:9443/logout");
    const afterLogout = await page.request.get(`${API_URL}/api/users/me/`);
    expect([401, 403]).toContain(afterLogout.status());
  });

  test("configures enforcement from God Mode", async ({ page }) => {
    await page.goto(ADMIN_URL);
    await page.locator('input[name="email"]').fill("recovery.e2e@example.com");
    await page.locator('input[name="password"]').fill("PlaneE2E!Recovery123");
    await page.getByRole("button", { name: /sign in/i }).click();
    await page.waitForURL(/\/god-mode\//);

    await page.goto(`${ADMIN_URL}/god-mode/authentication/sso`);
    const issuerInput = page.getByRole("textbox", { name: "Issuer URL" });
    await expect(issuerInput).toBeVisible();
    await expect(issuerInput).toHaveValue("https://127.0.0.1:9443");
    await page.getByRole("button", { name: "Enforce SSO" }).click();
    await expect(page.getByRole("button", { name: "Stop enforcing SSO" })).toBeVisible();
  });

  test("blocks legacy authentication while enforced and still permits SSO", async ({ page }) => {
    await page.goto(`${API_URL}/auth/sign-in/`);
    await page.waitForURL((url) => url.origin === WEB_URL);
    expect(page.url()).toContain("error_code=5126");

    await page.goto(WEB_URL);
    await expect(page.getByText(/with Local OIDC/i).first()).toBeVisible();
    await expect(page.locator('input[type="password"]')).toHaveCount(0);
    await page
      .getByText(/with Local OIDC/i)
      .first()
      .click();
    await page.waitForURL((url) => url.origin === WEB_URL && !url.searchParams.has("error_code"));
    const me = await page.request.get(`${API_URL}/api/users/me/`);
    expect(me.ok()).toBeTruthy();
  });
});
