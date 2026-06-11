import { expect, test } from "@playwright/test";

// backend の seed 状態に依存する (記事が 1 件以上必要) ため、空状態では skip。
test.describe("Watchlist add/remove flow", () => {
  // 前 run の残存 entry を落として冪等化し、上限で無限ループを明示 fail する。
  const MAX_CLEANUP_ITERATIONS = 50;
  test.beforeEach(async ({ page }) => {
    await page.goto("/watchlist");
    const removeButtons = page.getByRole("button", {
      name: "Remove from watchlist",
    });
    let remaining = await removeButtons.count();
    let iterations = 0;
    while (remaining > 0) {
      if (iterations >= MAX_CLEANUP_ITERATIONS) {
        throw new Error(
          `watchlist cleanup did not converge after ${MAX_CLEANUP_ITERATIONS} iterations (remaining=${remaining})`,
        );
      }
      await removeButtons.first().click();
      await expect(removeButtons).toHaveCount(remaining - 1);
      remaining -= 1;
      iterations += 1;
    }
  });

  test("記事を watchlist に追加して /watchlist で表示確認、最後に削除", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(
      page.getByRole("link", { name: "Vector ニュースへ" }),
    ).toBeVisible();

    // aria 状態で locator 集合が変わるため、同一 DOM node を追う。
    const firstUnwatchedLocator = page
      .locator('button[aria-pressed="false"]')
      .first();
    const candidateCount = await page
      .locator('button[aria-pressed="false"]')
      .count();
    test.skip(
      candidateCount === 0,
      "ニュース一覧上に未 watch 記事が無いため skip (seed 依存)",
    );
    const targetButton = await firstUnwatchedLocator.elementHandle();
    if (targetButton === null) {
      throw new Error("expected at least one unwatched button");
    }
    await targetButton.click();
    await expect
      .poll(async () => await targetButton.getAttribute("aria-pressed"))
      .toBe("true");

    await page.goto("/watchlist");
    await expect(
      page.getByRole("heading", { name: "ウォッチリスト" }),
    ).toBeVisible();
    await expect(page.getByText("No saved articles")).toHaveCount(0);

    await page
      .getByRole("button", { name: "Remove from watchlist" })
      .first()
      .click();
  });

  test("watchlist 追加後に aria-pressed が flicker しない", async ({
    page,
  }) => {
    // PR-Z2 regression guard: refreshed server data must not revert optimistic state.
    await page.goto("/");
    await expect(
      page.getByRole("link", { name: "Vector ニュースへ" }),
    ).toBeVisible();
    const candidateCount = await page
      .locator('button[aria-pressed="false"]')
      .count();
    test.skip(
      candidateCount === 0,
      "ニュース一覧上に未 watch 記事が無いため skip (seed 依存)",
    );
    // aria 状態で locator 集合が変わるため、同一 DOM node を追う。
    const targetButton = await page
      .locator('button[aria-pressed="false"]')
      .first()
      .elementHandle();
    if (targetButton === null) {
      throw new Error("expected at least one unwatched button");
    }
    await targetButton.click();
    await expect
      .poll(async () => await targetButton.getAttribute("aria-pressed"))
      .toBe("true");
    for (let i = 0; i < 5; i++) {
      await page.waitForTimeout(200);
      expect(await targetButton.getAttribute("aria-pressed")).toBe("true");
    }
    await page
      .getByRole("button", { name: "Remove from watchlist" })
      .first()
      .click();
  });
});
