import { expect, test } from "@playwright/test";

test("トレンドページの heading + 構造が描画される", async ({ page }) => {
  await page.goto("/trends");
  await expect(page.getByRole("heading", { name: "トレンド" })).toBeVisible();

  const emptyMessage = page.getByText("該当するワードはありません");
  const rankingHeading = page.getByText("言及数上位").first();

  await expect(async () => {
    const empty = await emptyMessage.count();
    const populated = await rankingHeading.count();
    expect(empty + populated).toBeGreaterThan(0);
  }).toPass({ timeout: 10_000 });
});
