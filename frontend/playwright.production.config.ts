import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:3100";
if (!["localhost", "127.0.0.1"].includes(new URL(baseURL).hostname)) {
  throw new Error("Production smoke tests require a local frontend");
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: /production-hydration\.spec\.ts$/,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    // ALBが付与する接続元ヘッダーを、専用のローカル本番イメージに渡す。
    extraHTTPHeaders: { "X-Forwarded-For": "127.0.0.1" },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
