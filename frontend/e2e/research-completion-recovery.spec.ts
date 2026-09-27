import { expect } from "@playwright/test";
import { test } from "./fixtures/research-completion-recovery";

test("完了通知を受け取らなくても再読み込みで確定回答と出典が表示される", async ({
  page,
  researchWithoutCompletionUpdates: research,
}) => {
  const { view } = research;

  await research.open();
  await research.showDraft();
  await expect(view.draft).toBeVisible();

  await research.saveCompletedAnswerToDatabase();
  await research.expectCompletionNotDelivered();
  await expect(view.runningRun).toBeVisible();
  await expect(view.draft).toBeVisible();
  await expect(view.finalAnswer).toHaveCount(0);

  await page.reload();

  await expect(view.completedRun).toBeVisible();
  await expect(view.finalAnswer).toBeVisible();
  await expect(view.finalAnswer).toHaveCount(1);
  await expect(view.draft).toHaveCount(0);
  await expect(view.answerSlot).toHaveCount(1);

  await view.sourcesButton.click();
  await expect(view.savedSource).toBeVisible();
});
