import { expect, test } from "@playwright/test";
import { USER } from "./fixtures/users";

// anon project: storageState なし。UI 経由 login の regression を凍結する。
test.describe("Login flow (UI 経由)", () => {
  test("正規 credential でダッシュボードへ遷移", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel("Email").fill(USER.email);
    await page.getByLabel("Password").fill(USER.password);
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL("/");
    await expect(
      page.getByRole("heading", { name: "Dashboard" }),
    ).toBeVisible();
  });

  test("無効 credential で error 表示・URL 維持", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel("Email").fill("nobody@e2e.local");
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByRole("alert")).toHaveText(
      /Invalid email or password/,
    );
    // login 画面に留まる (auto-redirect しない)
    await expect(page).toHaveURL(/\/auth\/login/);
  });
});
