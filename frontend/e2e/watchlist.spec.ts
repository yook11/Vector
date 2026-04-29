import { expect, test } from "@playwright/test";

// user project: e2e/.auth/user.json で programmatic auth 済。
// Dashboard 上で記事を watchlist に追加 → /watchlist で表示確認 → 削除して原状回復。
// backend の seed 状態に依存する (記事が 1 件以上必要) ため、空状態では skip。
test.describe("Watchlist add/remove flow", () => {
  test("記事を watchlist に追加して /watchlist で表示確認、最後に削除", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Dashboard" }),
    ).toBeVisible();

    const addButtons = page.getByRole("button", { name: "Add to watchlist" });
    const addCount = await addButtons.count();
    test.skip(
      addCount === 0,
      "Dashboard 上に未 watch 記事が無いため skip (seed 依存)",
    );

    const targetButton = addButtons.first();
    await targetButton.click();
    // optimistic update で即 aria-pressed=true に
    await expect(targetButton).toHaveAttribute("aria-pressed", "true");

    await page.goto("/watchlist");
    await expect(
      page.getByRole("heading", { name: "Watchlist" }),
    ).toBeVisible();
    // 追加した記事が表示されている (空 state ではない)
    await expect(page.getByText("No saved articles")).toHaveCount(0);

    // 原状回復: 最初の Remove ボタンを押す
    await page
      .getByRole("button", { name: "Remove from watchlist" })
      .first()
      .click();
  });
});
