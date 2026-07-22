import { expect, type Page, request, test } from "@playwright/test";
import { Pool, type PoolClient } from "pg";
import { poolConfigFromUrl } from "../src/lib/auth/pool-ssl";
import { USER } from "./fixtures/users";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const USER_STATE = "e2e/.auth/user.json";
const ADMIN_STATE = "e2e/.auth/admin.json";
const DATABASE_URL = process.env.AUTH_DATABASE_URL?.trim();
const AUTH_DB_MUTATION_ENABLED =
  Boolean(DATABASE_URL) && process.env.E2E_ALLOW_AUTH_DB_MUTATION === "true";
const PROVISION_EMAIL = "e2e-admin-provisioning-concurrency@example.com";
const PROVISION_NAME = "並行登録 E2E";

async function deleteProvisioningFixture(
  pool: Pool,
  email: string,
): Promise<void> {
  const client: PoolClient = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      `DELETE FROM auth.session
       WHERE "userId" IN (SELECT id FROM auth."user" WHERE email = $1)`,
      [email],
    );
    await client.query(
      `DELETE FROM auth.account
       WHERE "userId" IN (SELECT id FROM auth."user" WHERE email = $1)`,
      [email],
    );
    await client.query('DELETE FROM auth."user" WHERE email = $1', [email]);
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

async function fillProvisioningForm(page: Page): Promise<void> {
  await page.goto("/admin/users/new");
  await page.getByLabel("名前").fill(PROVISION_NAME);
  await page.getByLabel("メールアドレス").fill(PROVISION_EMAIL);
  await page.getByLabel("パスワード").fill(USER.password);
  await page.getByRole("checkbox", { name: "認証情報を控えました" }).check();
}

test.describe("Admin user provisioning", () => {
  test("既存ユーザー送信をduplicate-emailとして表示し入力を保持する", async ({
    page,
  }) => {
    await page.goto("/admin/users/new");

    await expect(page).toHaveURL(/\/admin\/users\/new$/);
    await expect(
      page.getByRole("heading", { name: "デモユーザーを登録" }),
    ).toBeVisible();
    const form = page.locator("form");
    await expect(
      form.locator('input[name]:not([name^="$ACTION_"])'),
    ).toHaveCount(3);
    await expect(form.locator('input[name="name"]')).toHaveCount(1);
    await expect(form.locator('input[name="email"]')).toHaveCount(1);
    await expect(form.locator('input[name="password"]')).toHaveCount(1);
    await expect(form.locator('input[name="role"]')).toHaveCount(0);

    const nameInput = page.getByLabel("名前");
    const emailInput = page.getByLabel("メールアドレス");
    const passwordInput = page.getByLabel("パスワード");
    const confirmation = page.getByRole("checkbox", {
      name: "認証情報を控えました",
    });
    const submit = page.getByRole("button", { name: "一般ユーザーを登録" });

    await expect(submit).toBeDisabled();
    await nameInput.fill("既存 E2E ユーザー");
    await emailInput.fill(USER.email);
    await passwordInput.fill(USER.password);
    await expect(submit).toBeDisabled();
    await confirmation.check();
    await expect(submit).toBeEnabled();

    await submit.click();

    await expect(
      page.getByText("このメールアドレスは登録済みです。"),
    ).toBeVisible();
    await expect(page.getByText("入力内容を確認してください。")).toHaveCount(0);
    await expect(nameInput).toHaveValue("既存 E2E ユーザー");
    await expect(emailInput).toHaveValue(USER.email);
    await expect(passwordInput).toHaveValue(USER.password);
    await expect(confirmation).toBeChecked();
  });

  test("不正な入力をServer Actionで検証し日本語エラーと入力を保持する", async ({
    page,
  }) => {
    await page.goto("/admin/users/new");

    const nameInput = page.getByLabel("名前");
    const emailInput = page.getByLabel("メールアドレス");
    const passwordInput = page.getByLabel("パスワード");
    const confirmation = page.getByRole("checkbox", {
      name: "認証情報を控えました",
    });
    const submit = page.getByRole("button", { name: "一般ユーザーを登録" });

    await nameInput.fill("入力不正 E2E");
    await emailInput.fill("not-an-email");
    await passwordInput.fill("short");
    await confirmation.check();
    await submit.click();

    await expect(page.getByText("入力内容を確認してください。")).toBeVisible();
    await expect(
      page.getByText("有効なメールアドレスを入力してください。"),
    ).toBeVisible();
    await expect(
      page.getByText("パスワードは8文字以上で入力してください。"),
    ).toBeVisible();
    await expect(nameInput).toHaveValue("入力不正 E2E");
    await expect(emailInput).toHaveValue("not-an-email");
    await expect(passwordInput).toHaveValue("short");
    await expect(confirmation).toBeChecked();
  });

  test("一般ユーザーはadmin provisioning pageを表示できない", async ({
    browser,
  }) => {
    const context = await browser.newContext({ storageState: USER_STATE });
    try {
      const userPage = await context.newPage();

      await userPage.goto("/admin/users/new");

      await expect(userPage).toHaveURL(new URL("/", BASE).toString());
      await expect(
        userPage.getByRole("heading", { name: "デモユーザーを登録" }),
      ).toHaveCount(0);
    } finally {
      await context.close();
    }
  });
});

test.describe("Admin user provisioning integration", () => {
  let pool: Pool | undefined;

  test.skip(
    !AUTH_DB_MUTATION_ENABLED,
    "AUTH_DATABASE_URLとE2E_ALLOW_AUTH_DB_MUTATION=trueの両方がない環境では実DB provisioning integrationを実行しない",
  );

  test.beforeEach(async () => {
    if (!AUTH_DB_MUTATION_ENABLED || !DATABASE_URL) return;

    pool = new Pool(poolConfigFromUrl(DATABASE_URL));
    try {
      await deleteProvisioningFixture(pool, PROVISION_EMAIL);
    } catch (error) {
      await pool.end();
      pool = undefined;
      throw error;
    }
  });

  test.afterEach(async () => {
    if (!pool) return;

    try {
      await deleteProvisioningFixture(pool, PROVISION_EMAIL);
    } finally {
      await pool.end();
      pool = undefined;
    }
  });

  test("並行登録を1件に保ち実credentialでsign-inできる", async ({
    browser,
  }) => {
    if (!pool) throw new Error("Provisioning integration pool is unavailable.");

    const contexts = await Promise.all([
      browser.newContext({ storageState: ADMIN_STATE }),
      browser.newContext({ storageState: ADMIN_STATE }),
    ]);
    const pages = await Promise.all(
      contexts.map((context) => context.newPage()),
    );

    try {
      await Promise.all(pages.map(fillProvisioningForm));
      await Promise.all(
        pages.map((page) =>
          page.getByRole("button", { name: "一般ユーザーを登録" }).click(),
        ),
      );

      const outcomes = await Promise.all(
        pages.map(async (page) => {
          const success = page.getByText("一般ユーザーを登録しました", {
            exact: true,
          });
          const duplicate = page.getByText(
            "このメールアドレスは登録済みです。",
            { exact: true },
          );
          await expect(success.or(duplicate)).toBeVisible();

          const successVisible = await success.isVisible();
          const duplicateVisible = await duplicate.isVisible();
          expect(Number(successVisible) + Number(duplicateVisible)).toBe(1);
          return successVisible ? "success" : "duplicate";
        }),
      );

      expect([...outcomes].sort()).toEqual(["duplicate", "success"]);
      const successPage = pages[outcomes.indexOf("success")];
      if (!successPage) throw new Error("Success page was not found.");
      const status = successPage
        .getByRole("status")
        .filter({ hasText: PROVISION_EMAIL });
      await expect(status).toContainText(PROVISION_EMAIL);
      await expect(status).not.toContainText(USER.password);
      await expect(successPage.getByLabel("パスワード")).toHaveValue("");

      const databaseState = await pool.query<{
        accountCount: number;
        accountId: string;
        accountUserId: string;
        providerId: string;
        role: string;
        sessionCount: number;
        userCount: number;
        userId: string;
      }>(
        `SELECT
           (SELECT COUNT(*)::int FROM auth."user" WHERE email = $1) AS "userCount",
           (SELECT COUNT(*)::int FROM auth.account
             WHERE "userId" IN (SELECT id FROM auth."user" WHERE email = $1)) AS "accountCount",
           (SELECT COUNT(*)::int FROM auth.session
             WHERE "userId" IN (SELECT id FROM auth."user" WHERE email = $1)) AS "sessionCount",
           u.id::text AS "userId",
           u.role,
           a."accountId",
           a."providerId",
           a."userId"::text AS "accountUserId"
         FROM auth."user" AS u
         JOIN auth.account AS a ON a."userId" = u.id
         WHERE u.email = $1`,
        [PROVISION_EMAIL],
      );

      expect(databaseState.rows).toHaveLength(1);
      const databaseRow = databaseState.rows[0];
      if (!databaseRow)
        throw new Error("Provisioned database row was not found.");
      expect(databaseRow).toMatchObject({
        userCount: 1,
        accountCount: 1,
        sessionCount: 0,
        role: "user",
        providerId: "credential",
      });
      expect(databaseRow.accountId).toBe(databaseRow.userId);
      expect(databaseRow.accountUserId).toBe(databaseRow.userId);

      const anonymous = await request.newContext({
        baseURL: BASE,
        extraHTTPHeaders: { Origin: BASE },
      });
      try {
        const response = await anonymous.post("/api/auth/sign-in/email", {
          data: { email: PROVISION_EMAIL, password: USER.password },
        });
        expect(response.status()).toBe(200);

        const sessionState = await pool.query<{ sessionCount: number }>(
          `SELECT COUNT(*)::int AS "sessionCount"
           FROM auth.session
           WHERE "userId" IN (SELECT id FROM auth."user" WHERE email = $1)`,
          [PROVISION_EMAIL],
        );
        const sessionRow = sessionState.rows[0];
        if (!sessionRow)
          throw new Error("Provisioned session row was not found.");
        expect(sessionRow.sessionCount).toBe(1);
      } finally {
        await anonymous.dispose();
      }
    } finally {
      await Promise.all(contexts.map((context) => context.close()));
    }
  });
});
