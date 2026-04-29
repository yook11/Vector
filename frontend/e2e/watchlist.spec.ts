import { expect, test } from "@playwright/test";

// user project: e2e/.auth/user.json で programmatic auth 済。
// Dashboard 上で記事を watchlist に追加 → /watchlist で表示確認 → 削除して原状回復。
// backend の seed 状態に依存する (記事が 1 件以上必要) ため、空状態では skip。
test.describe("Watchlist add/remove flow", () => {
  // 前 run で fail し残った entry をクリーンアップしてから本テストに入る。
  // /watchlist で全 Remove を順に click することで実現する (Dashboard 経由だと
  // aria-pressed=true のカードが上位に並ぶ等の page state を仮定できないため)。
  test.beforeEach(async ({ page }) => {
    await page.goto("/watchlist");
    const removeButtons = page.getByRole("button", {
      name: "Remove from watchlist",
    });
    let remaining = await removeButtons.count();
    while (remaining > 0) {
      await removeButtons.first().click();
      // Server Action 経由の reflect (optimistic 解除 + revalidation) を待つ
      await expect(removeButtons).toHaveCount(remaining - 1);
      remaining -= 1;
    }
  });

  test("記事を watchlist に追加して /watchlist で表示確認、最後に削除", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Dashboard" }),
    ).toBeVisible();

    // click 後に aria-label が "Add" → "Remove" に変わるため、label ベースの
    // lazy ロケータは別ボタンに resolve してしまう。Playwright の locator は
    // 評価のたびに DOM を再走査するので、stable な reference を得るために
    // `elementHandle()` で固定する。useOptimistic は同じ React コンポーネント
    // ツリーを再 render するだけなので、DOM ノード自体は同一性を保ち、handle
    // も valid のまま。
    const firstUnwatchedLocator = page
      .locator('button[aria-pressed="false"]')
      .first();
    const candidateCount = await page
      .locator('button[aria-pressed="false"]')
      .count();
    test.skip(
      candidateCount === 0,
      "Dashboard 上に未 watch 記事が無いため skip (seed 依存)",
    );
    const targetButton = await firstUnwatchedLocator.elementHandle();
    if (targetButton === null) {
      throw new Error("expected at least one unwatched button");
    }
    await targetButton.click();
    // optimistic update で即 aria-pressed=true に (同じ DOM node を見続ける)
    await expect
      .poll(async () => await targetButton.getAttribute("aria-pressed"))
      .toBe("true");

    await page.goto("/watchlist");
    await expect(
      page.getByRole("heading", { name: "Watchlist" }),
    ).toBeVisible();
    // 追加した記事が表示されている (空 state ではない)
    await expect(page.getByText("No saved articles")).toHaveCount(0);

    // 原状回復: 最初の Remove ボタンを押す (cleanup は beforeEach 側でも
    // 拾われるが、テスト末尾でも試行して trace を見やすくする)
    await page
      .getByRole("button", { name: "Remove from watchlist" })
      .first()
      .click();
  });
});
