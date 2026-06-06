import { expect, test } from "@playwright/test";
import { USER } from "./fixtures/users";

// anon project: storageState なし。UI 経由 login の regression を凍結する。
test.describe("Login flow (UI 経由)", () => {
  test("正規 credential でダッシュボードへ遷移", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel("Email").fill(USER.email);
    await page.getByLabel("Password").fill(USER.password);
    await page.getByRole("button", { name: "Sign in" }).click();

    // useActionState + useEffect 経由で router.push する経路は HTTP roundtrip
    // + state 更新 + effect 発火が直列で、CI の next dev cold start 時に
    // default 5s を超えうる。timeout を伸ばして flake を構造的に消す。
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

    // `getByRole("alert")` だと Next.js の `#__next-route-announcer__` (空文字)
    // と LoginForm のエラー div が両方 match して strict mode violation になる。
    // PR-Z4 の field-level error refactor で credential 不正の formError は
    // `#login-form-error` (旧 `#login-error` から rename) に出力される。
    await expect(page.locator("#login-form-error")).toHaveText(
      /Invalid email or password/,
    );
    // login 画面に留まる (auto-redirect しない)
    await expect(page).toHaveURL(/\/auth\/login/);
  });
});
