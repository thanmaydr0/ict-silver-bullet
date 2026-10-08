import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8791",
    headless: true,
    viewport: { width: 1440, height: 1100 },
  },
  webServer: {
    command: "python ../tests/brain/dashboard_browser_fixture.py",
    url: "http://127.0.0.1:8791/healthz",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
