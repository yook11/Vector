import { expect, test } from "@playwright/test";
import { USER } from "./fixtures/users";

test.describe("Login flow (UI 経由)", () => {
  test("正規 credential でダッシュボードへ遷移", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel("メールアドレス").fill(USER.email);
    await page.getByLabel("パスワード").fill(USER.password);
    await page.getByRole("button", { name: "ログイン" }).click();

    // Next dev cold start では Server Action 完了から router.push まで 5s を超えうる。
    await expect(page).toHaveURL("/", { timeout: 15_000 });
    await expect(
      page.getByRole("link", { name: "Vector ニュースへ" }),
    ).toBeVisible();
  });

  test("無効 credential で error 表示・URL 維持", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel("メールアドレス").fill("nobody@e2e.local");
    await page.getByLabel("パスワード").fill("wrong-password");
    await page.getByRole("button", { name: "ログイン" }).click();

    // Next.js route announcer との role 衝突を避け、form error の出力先を直接見る。
    await expect(page.locator("#login-form-error")).toHaveText(
      "メールアドレスまたはパスワードが正しくありません。",
    );
    await expect(page).toHaveURL(/\/auth\/login/);
  });
});
