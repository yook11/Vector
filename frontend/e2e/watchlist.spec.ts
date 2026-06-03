import { expect, test } from "@playwright/test";

// user project: e2e/.auth/user.json で programmatic auth 済。
// ニュース一覧で記事を watchlist に追加 → /watchlist で表示確認 → 削除して原状回復。
// backend の seed 状態に依存する (記事が 1 件以上必要) ため、空状態では skip。
test.describe("Watchlist add/remove flow", () => {
  // 前 run で fail し残った entry をクリーンアップしてから本テストに入る。
  // /watchlist で全 Remove を順に click することで実現する (ニュース一覧経由だと
  // aria-pressed=true のカードが上位に並ぶ等の page state を仮定できないため)。
  // 安全弁として最大反復回数を `MAX_CLEANUP_ITERATIONS` に制限する。万が一
  // Server Action が `toHaveCount(remaining - 1)` を満たさなくなる回帰が
  // 入った際に、E2E が無限ループに陥らず明示的に fail する。
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
      // Server Action 経由の reflect (optimistic 解除 + revalidation) を待つ
      await expect(removeButtons).toHaveCount(remaining - 1);
      remaining -= 1;
      iterations += 1;
    }
  });

  test("記事を watchlist に追加して /watchlist で表示確認、最後に削除", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "ニュース" })).toBeVisible();

    // 注: 通常の Playwright ベストプラクティスは locator-based assertion で
    // あり `elementHandle()` は legacy API。ただしこのケースでは click 後に
    // aria-pressed や aria-label が変わって locator の評価対象 (DOM 集合) が
    // 変動するため、locator では「最初に identify した同一ノード」を追跡できない。
    // useOptimistic は同じ React ツリーを再 render するのみで DOM ノード自体は
    // 同一性を保つので、`elementHandle()` で stable な reference を取って
    // click 前後の aria-pressed 推移を観測する。data-testid を付与するという
    // 案もあるが、本番 DOM に test 専用 attribute を増やす副作用を避けたい。
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

  test("watchlist 追加後に aria-pressed が flicker しない", async ({
    page,
  }) => {
    // PR-Z2 で `revalidateTag(_, "max") + refresh()` から `updateTag` 単独に
    // 移行した。`updateTag` は同一 Server Action リクエスト内で server data
    // cache を吹き飛ばし current route を再生成するため、`useOptimistic` の
    // base が `getWatchlistIds` の新値で同期される前に "false" に戻る flicker
    // が起きないことが要件。1 秒間 (200ms x 5 回) `aria-pressed` を観測して
    // 一度も "true" 以外を返さないことを確認する。
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "ニュース" })).toBeVisible();
    const candidateCount = await page
      .locator('button[aria-pressed="false"]')
      .count();
    test.skip(
      candidateCount === 0,
      "ニュース一覧上に未 watch 記事が無いため skip (seed 依存)",
    );
    // `elementHandle()` を使う理由は前テストのコメントを参照。click 前後の
    // aria-pressed を同一 DOM ノードで観測するために stable な reference が必要。
    const targetButton = await page
      .locator('button[aria-pressed="false"]')
      .first()
      .elementHandle();
    if (targetButton === null) {
      throw new Error("expected at least one unwatched button");
    }
    await targetButton.click();
    // optimistic で即 "true" に変わることを確認した上で、Server Action 完了後
    // も "true" を維持し続けることを 1 秒間観測する。
    await expect
      .poll(async () => await targetButton.getAttribute("aria-pressed"))
      .toBe("true");
    for (let i = 0; i < 5; i++) {
      await page.waitForTimeout(200);
      expect(await targetButton.getAttribute("aria-pressed")).toBe("true");
    }
    // 原状回復
    await page
      .getByRole("button", { name: "Remove from watchlist" })
      .first()
      .click();
  });
});
