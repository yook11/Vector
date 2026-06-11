import { expect, test } from "@playwright/test";
import { USER } from "./fixtures/users";

test.describe("Login flow (UI 経由)", () => {
  test("正規 credential でダッシュボードへ遷移", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel("Email").fill(USER.email);
    await page.getByLabel("Password").fill(USER.password);
    await page.getByRole("button", { name: "Sign in" }).click();

    // Next dev cold start では Server Action 完了から router.push まで 5s を超えうる。
    await expect(page).toHaveURL("/", { timeout: 15_000 });
    await expect(
      page.getByRole("link", { name: "Vector ニュースへ" }),
    ).toBeVisible();
  });

  test("無効 credential で error 表示・URL 維持", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel("Email").fill("nobody@e2e.local");
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    // Next.js route announcer との role 衝突を避け、form error の出力先を直接見る。
    await expect(page.locator("#login-form-error")).toHaveText(
      /Invalid email or password/,
    );
    await expect(page).toHaveURL(/\/auth\/login/);
  });
});
