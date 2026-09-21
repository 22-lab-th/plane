import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e/files",
  testMatch: "*.spec.ts",
  // The Files route is compiled by the dev server on its first hit, and on a cold
  // container that compile can outlast any measured transition. `globalSetup` pays it
  // once, before the first test starts, so the tests below run against a warm server and
  // their timeouts only have to cover the work they assert.
  globalSetup: "./e2e/files/global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  // A container serving the compiled app is slower than a laptop for every request, and
  // the upload spec moves real bytes through the store: the ceiling is raised so a slow
  // machine reports its own failure rather than the harness's timeout.
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { outputFolder: "playwright-report/files", open: "never" }]],
  use: {
    baseURL: process.env.E2E_WEB_URL || "http://127.0.0.1:3000",
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
