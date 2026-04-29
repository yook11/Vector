import { expect, test } from "@playwright/test";

// user project: dashboard 上の SearchBar と URL `?q=` 同期を凍結する。
// 検索結果の中身 (件数・ヒット記事) は backend データに依存するため検証しない。
// URL の同期と clear 操作のみを保証する。
test.describe("Search debounce & URL sync", () => {
  test("debounce 500ms 後に URL ?q= に同期", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Search articles").fill("openai");

    // debounce timer (500ms) 後に navigate される
    await page.waitForURL(/[?&]q=openai/);
  });

  test("Enter キーで debounce skip して即時 navigate", async ({ page }) => {
    await page.goto("/");
    const input = page.getByLabel("Search articles");
    await input.fill("ai");
    await input.press("Enter");
    await page.waitForURL(/[?&]q=ai/);
  });

  test("clear ボタンで q を解除", async ({ page }) => {
    await page.goto("/?q=initial");
    await expect(page.getByLabel("Search articles")).toHaveValue("initial");
    await page.getByLabel("Clear search").click();

    // q が URL から消える
    await expect(page).not.toHaveURL(/[?&]q=/);
    await expect(page.getByLabel("Search articles")).toHaveValue("");
  });
});
