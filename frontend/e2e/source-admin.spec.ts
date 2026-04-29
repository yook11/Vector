import { expect, test } from "@playwright/test";

// admin project: e2e/.auth/admin.json で admin 権限済。
// SourceTable の Switch toggle が永続することを reload で確認、`afterEach` で
// 原状回復することで test の冪等性を保つ。
test.describe("Source admin (toggle 永続化)", () => {
  test("Switch toggle 後 reload で状態が維持される (原状回復付き)", async ({
    page,
  }) => {
    await page.goto("/settings");
    await expect(
      page.getByRole("heading", { name: "Settings" }),
    ).toBeVisible();

    const switches = page.getByRole("switch");
    const switchCount = await switches.count();
    test.skip(
      switchCount === 0,
      "登録済み source が無いため skip (seed 依存)",
    );

    const target = switches.first();
    await expect(target).toBeVisible();
    const initial = await target.getAttribute("aria-checked");

    try {
      await target.click();
      // toggle 完了を aria-checked の変化で待つ
      await expect(target).not.toHaveAttribute(
        "aria-checked",
        initial ?? "false",
      );

      await page.reload();
      const persisted = await page
        .getByRole("switch")
        .first()
        .getAttribute("aria-checked");
      expect(persisted).not.toBe(initial);
    } finally {
      // 原状回復: 現在の aria-checked が initial と異なれば再 toggle
      const current = await page
        .getByRole("switch")
        .first()
        .getAttribute("aria-checked");
      if (current !== initial) {
        await page.getByRole("switch").first().click();
      }
    }
  });
});
