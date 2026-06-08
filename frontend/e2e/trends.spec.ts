import { expect, test } from "@playwright/test";

// user project: /trends を開いて見出しと "ランキング / 空 state" の
// いずれかが表示されることを保証する。具体的な trend 数は seed 依存なので避ける。
test("トレンドページの heading + 構造が描画される", async ({ page }) => {
  await page.goto("/trends");
  await expect(page.getByRole("heading", { name: "トレンド" })).toBeVisible();

  // empty state (snapshot 無し) または categories の片方が必ず描画される
  const emptyMessage = page.getByText("トレンドはまだ生成されていません");
  const rankingHeading = page.getByText("よく言及").first();

  await expect(async () => {
    const empty = await emptyMessage.count();
    const populated = await rankingHeading.count();
    expect(empty + populated).toBeGreaterThan(0);
  }).toPass({ timeout: 10_000 });
});
