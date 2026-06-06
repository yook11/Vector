import { expect, test } from "@playwright/test";

// anon project: storageState なし。
// 新規 email を `${Date.now()}@e2e.local` で都度生成して unique 化することで、
// 同 user の再 register による fail (USER_ALREADY_EXISTS) を構造的に避ける。
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

    // Zod 検証失敗で alert 表示、URL は register に留まる
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page).toHaveURL(/\/auth\/register/);
  });
});
