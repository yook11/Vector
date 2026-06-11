import { expect, test } from "@playwright/test";

test.describe("Register flow (UI 経由)", () => {
  test("新規ユーザ登録 → ダッシュボード遷移", async ({ page }) => {
    const uniqueEmail = `e2e+${Date.now()}@e2e.local`;

    await page.goto("/auth/register");
    await page.getByLabel("Display Name").fill("E2E Test User");
    await page.getByLabel("Email").fill(uniqueEmail);
    await page.getByLabel("Password").fill("Password123!");
    await page.getByRole("button", { name: "Create account" }).click();

    await expect(page).toHaveURL("/");
    await expect(
      page.getByRole("link", { name: "Vector ニュースへ" }),
    ).toBeVisible();
  });

  test("password 8 文字未満で client-side validation 失敗", async ({
    page,
  }) => {
    await page.goto("/auth/register");
    await page.getByLabel("Display Name").fill("E2E");
    await page.getByLabel("Email").fill(`e2e+${Date.now()}@e2e.local`);
    await page.getByLabel("Password").fill("short");
    await page.getByRole("button", { name: "Create account" }).click();

    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page).toHaveURL(/\/auth\/register/);
  });
});
