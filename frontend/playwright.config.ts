import { defineConfig, devices } from "@playwright/test";

// Phase 3 PR-5: ローカル only の E2E。CI には乗せず、developer は事前に
// `docker compose up -d` + `npm run dev` で backend + frontend を起動済みの
// 前提で `npm run test:e2e` を実行する。
//
// programmatic auth: `auth.setup.ts` が Better Auth `/api/auth/sign-in/email`
// に POST して session cookie を取得し、`e2e/.auth/{user,admin}.json` に
// storageState を保存する。各 spec はそれを project の `use.storageState` で
// 再利用する。
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "on-first-retry",
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
  projects: [
    {
      name: "setup",
      testMatch: /.*\.setup\.ts$/,
    },
    {
      name: "anon",
      use: { ...devices["Desktop Chrome"] },
      testMatch: [/login\.spec\.ts$/, /register\.spec\.ts$/],
    },
    {
      name: "user",
      use: {
        ...devices["Desktop Chrome"],
        storageState: "e2e/.auth/user.json",
      },
      dependencies: ["setup"],
      testIgnore: [
        /login\.spec\.ts$/,
        /register\.spec\.ts$/,
        /source-admin\.spec\.ts$/,
        /.*\.setup\.ts$/,
      ],
    },
    {
      name: "admin",
      use: {
        ...devices["Desktop Chrome"],
        storageState: "e2e/.auth/admin.json",
      },
      dependencies: ["setup"],
      testMatch: /source-admin\.spec\.ts$/,
    },
  ],
});
