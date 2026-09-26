import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.VLYTICS_LIVE_E2E_BASE_URL;
if (!baseURL) throw new Error("VLYTICS_LIVE_E2E_BASE_URL is required");

export default defineConfig({
  testDir: "./tests/live-e2e",
  outputDir: "./tests/live-e2e/artifacts/test-results",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL,
    screenshot: "only-on-failure",
    trace: "off",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
