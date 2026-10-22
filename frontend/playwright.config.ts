import { defineConfig, devices } from "@playwright/test";

/**
 * Smoke E2E (login, 2FA, dashboard, tempo real) against a running stack.
 *
 * - E2E_BASE_URL: frontend URL (default http://localhost:3000). The frontend must proxy /api to a backend with an
 *   empty or disposable database (the test registers new users).
 * - E2E_START_FRONTEND=1: start `next start` here (CI); the backend must already be running.
 * - PW_CHANNEL=chrome: use an installed Google Chrome instead of the bundled Chromium.
 */
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL,
    locale: "pt-BR",
    timezoneId: "America/Sao_Paulo",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], channel: process.env.PW_CHANNEL } },
    { name: "mobile", use: { ...devices["Pixel 7"], channel: process.env.PW_CHANNEL } },
  ],
  webServer: process.env.E2E_START_FRONTEND
    ? {
        command: `npx next start -p ${new URL(baseURL).port || 3000}`,
        url: baseURL,
        timeout: 120_000,
        reuseExistingServer: false,
      }
    : undefined,
});
