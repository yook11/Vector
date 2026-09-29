import { expect, test } from "@playwright/test";
import { Pool } from "pg";
import { poolConfigFromUrl } from "../src/lib/auth/pool-ssl";
import { deleteAuthUserByEmail } from "./fixtures/auth-db";

const DATABASE_URL = process.env.AUTH_DATABASE_URL?.trim();
const AUTH_DB_MUTATION_ENABLED =
  Boolean(DATABASE_URL) && process.env.E2E_ALLOW_AUTH_DB_MUTATION === "true";
const SIGN_UP_EMAIL = "e2e-public-signup@example.com";
const SIGN_UP_PASSWORD = "Password123!";

test.describe("Register page", () => {
  test("メールアドレスとパスワードの登録フォームとログイン導線を表示する", async ({
    page,
  }) => {
    await page.goto("/auth/register");

    await expect(page.getByRole("heading", { name: "新規登録" })).toBeVisible();
    await expect(page.getByLabel("メールアドレス")).toBeVisible();
    await expect(page.getByLabel("パスワード")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "アカウントを作成" }),
    ).toBeEnabled();
    await expect(page.getByRole("link", { name: "ログイン" })).toHaveAttribute(
      "href",
      "/auth/login",
    );
  });
});

test.describe("Public signup integration", () => {
  let pool: Pool | undefined;

  test.skip(
    !AUTH_DB_MUTATION_ENABLED,
    "AUTH_DATABASE_URLとE2E_ALLOW_AUTH_DB_MUTATION=trueの両方がない環境では実DBへの公開登録を実行しない",
  );

  test.beforeEach(async () => {
    if (!AUTH_DB_MUTATION_ENABLED || !DATABASE_URL) return;

    pool = new Pool(poolConfigFromUrl(DATABASE_URL));
    try {
      await deleteAuthUserByEmail(pool, SIGN_UP_EMAIL);
    } catch (error) {
      await pool.end();
      pool = undefined;
      throw error;
    }
  });

  test.afterEach(async () => {
    if (!pool) return;

    try {
      await deleteAuthUserByEmail(pool, SIGN_UP_EMAIL);
    } finally {
      await pool.end();
      pool = undefined;
    }
  });

  // 登録の rate limit は IP あたり 60 秒に 5 回なので、実登録はこの 1 回に留める。
  test("登録するとログイン状態でトップへ移動する", async ({ page }) => {
    await page.goto("/auth/register");
    await page.getByLabel("メールアドレス").fill(SIGN_UP_EMAIL);
    await page.getByLabel("パスワード").fill(SIGN_UP_PASSWORD);
    await page.getByRole("button", { name: "アカウントを作成" }).click();

    // Next dev cold start では登録完了から router.push まで 5s を超えうる。
    await expect(page).toHaveURL("/", { timeout: 15_000 });
    await expect(page.getByText(SIGN_UP_EMAIL).first()).toBeVisible();
  });
});
