import { expect, test } from "@playwright/test";

// user project: /weekly-trends を開いて見出しと "Hot Entities / 空 state" の
// いずれかが表示されることを保証する。具体的な trend 数は seed 依存なので避ける。
test("Weekly Trends ページの heading + 構造が描画される", async ({ page }) => {
  await page.goto("/weekly-trends");
  await expect(
    page.getByRole("heading", { name: "Weekly Trends" }),
  ).toBeVisible();

  // empty state (snapshot 無し) または categories の片方が必ず描画される
  const emptyMessage = page.getByText(
    "週次トレンドはまだ生成されていません",
  );
  const hotEntitiesHeading = page.getByText("Hot Entities").first();

  await expect(async () => {
    const empty = await emptyMessage.count();
    const populated = await hotEntitiesHeading.count();
    expect(empty + populated).toBeGreaterThan(0);
  }).toPass({ timeout: 10_000 });
});
