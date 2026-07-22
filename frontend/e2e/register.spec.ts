import { expect, test } from "@playwright/test";

test.describe("Register page", () => {
  test("招待制の案内とログイン導線だけを表示する", async ({ page }) => {
    await page.goto("/auth/register");

    await expect(page.getByText("招待制で運用しています")).toBeVisible();
    await expect(
      page.getByText("現在、一般向けの新規登録は受け付けていません。"),
    ).toBeVisible();
    await expect(
      page.getByText("アカウントをお持ちの方はログインしてください。"),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "ログイン" })).toHaveAttribute(
      "href",
      "/auth/login",
    );
    await expect(page.locator("form")).toHaveCount(0);
    await expect(page.getByRole("textbox")).toHaveCount(0);
    await expect(page.locator('input[type="email"]')).toHaveCount(0);
    await expect(page.locator('input[type="password"]')).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "アカウントを作成" }),
    ).toHaveCount(0);
  });
});
