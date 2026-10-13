import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/production", workers: 1,
  use: { baseURL: "http://127.0.0.1:3108" },
  webServer: [
    { command: "node tests/mock-backend.mjs", url: "http://127.0.0.1:8107/health", reuseExistingServer: false, env: { KNOWFLOW_M51_FIXTURE: "1" } },
    { command: "npm run start -- --port 3108", url: "http://127.0.0.1:3108/login", reuseExistingServer: false, env: { KNOWFLOW_API_URL: "http://127.0.0.1:8107", KNOWFLOW_WEB_ORIGIN: "https://knowflow.example", KNOWFLOW_TEST_HTTP: "1", NEXT_TELEMETRY_DISABLED: "1" } },
  ],
});
