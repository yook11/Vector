// E2E test 用の seed user 認証情報。
// dev defaults は backend の seed migration で投入する想定の固定値。
// CI / 別環境向けには env で override する。
export const USER = {
  email: process.env.E2E_USER_EMAIL ?? "e2e@example.com",
  password: process.env.E2E_USER_PASSWORD ?? "Password123!",
} as const;

export const ADMIN_USER = {
  email: process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com",
  password: process.env.E2E_ADMIN_PASSWORD ?? "Password123!",
} as const;
