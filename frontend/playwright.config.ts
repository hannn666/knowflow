import { defineConfig, devices } from "@playwright/test";
const real = process.env.M51_REAL === "1";
export default defineConfig({
  testDir: "./tests",
  testIgnore: ["**/production/**","**/live/**"],
  timeout: 45000,
  expect: { timeout: 12000 },
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL: "http://127.0.0.1:3107",
    trace: "off",
    screenshot: "off",
    launchOptions: {
      executablePath:
        process.env.KNOWFLOW_BROWSER ??
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    },
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    {
      name: "mobile",
      use: {
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
  webServer: [
    {
      command: real
        ? "..\\.venv\\Scripts\\python.exe tests/real_backend.py"
        : "node tests/mock-backend.mjs",
      url: "http://127.0.0.1:8107/health",
      reuseExistingServer: false,
      timeout: 180000,
      env: { KNOWFLOW_M51_FIXTURE: "1" },
    },
    {
      command: "npm run dev -- --port 3107",
      url: "http://127.0.0.1:3107/login",
      reuseExistingServer: false,
      timeout: 120000,
      env: {
        KNOWFLOW_API_URL: "http://127.0.0.1:8107",
        KNOWFLOW_WEB_ORIGIN: "http://127.0.0.1:3107",
        NEXT_TELEMETRY_DISABLED: "1",
      },
    },
  ],
});
