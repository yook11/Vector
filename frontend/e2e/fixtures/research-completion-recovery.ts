import {
  test as base,
  type ConsoleMessage,
  expect,
  type Page,
} from "@playwright/test";
import { RESEARCH_CONTINUITY } from "./research";
import { installResearchContinuityBrowserHarness } from "./research-continuity";
import {
  completeResearchContinuity,
  resetResearchContinuity,
  resetResearchRateLimits,
} from "./research-runtime";

function completionRecoveryView(page: Page) {
  const fixture = RESEARCH_CONTINUITY.closed;
  const turn = page.locator(`[data-research-run-id="${fixture.activeRunId}"]`);
  const sources = page.getByRole("complementary", { name: "ソース" });

  return {
    heading: page.getByRole("heading", { name: fixture.title }),
    runningRun: page.locator(
      `[data-research-run-id="${fixture.activeRunId}"][data-research-persisted-status="running"]`,
    ),
    completedRun: page.locator(
      `[data-research-run-id="${fixture.activeRunId}"][data-research-persisted-status="completed"]`,
    ),
    draft: turn.getByText("E2E continuity live draft marker 1", {
      exact: false,
    }),
    finalAnswer: turn.getByText(fixture.completedActiveAnswerMarker),
    answerSlot: turn.getByTestId("research-answer-slot"),
    sourcesButton: page.locator(
      'button[aria-expanded]:has-text("ソース"):visible',
    ),
    savedSource: sources.locator(
      'a[href="https://example.com/e2e/research-continuity-closed/completed-source-1"]',
    ),
  };
}

interface ResearchWithoutCompletionUpdates {
  view: ReturnType<typeof completionRecoveryView>;
  open: () => Promise<void>;
  showDraft: () => Promise<void>;
  saveCompletedAnswerToDatabase: () => Promise<void>;
  expectCompletionNotDelivered: () => Promise<void>;
}

export const test = base.extend<{
  researchWithoutCompletionUpdates: ResearchWithoutCompletionUpdates;
}>({
  researchWithoutCompletionUpdates: async ({ page }, use, testInfo) => {
    testInfo.slow();
    await resetResearchRateLimits();
    await resetResearchContinuity("closed");
    await page.setViewportSize({ width: 1440, height: 900 });
    const fixture = RESEARCH_CONTINUITY.closed;
    const harness = await installResearchContinuityBrowserHarness(
      page,
      fixture,
    );
    const view = completionRecoveryView(page);
    const errors: string[] = [];
    const onConsole = (message: ConsoleMessage) => {
      if (message.type() === "error") errors.push(message.text());
    };
    const onPageError = (error: Error) => errors.push(error.message);
    page.on("console", onConsole);
    page.on("pageerror", onPageError);

    try {
      await use({
        view,
        open: async () => {
          await page.goto(`/research/${fixture.threadId}`);
          await expect(view.heading).toBeVisible();
        },
        showDraft: harness.emitDraft,
        saveCompletedAnswerToDatabase: () =>
          completeResearchContinuity("closed"),
        expectCompletionNotDelivered: async () => {
          const stats = await harness.stats();
          expect(stats).toMatchObject({
            draftEventsSent: 1,
            terminalEventsSent: 0,
          });
          expect(stats.targetPollStatuses).not.toContain("completed");
        },
      });
      expect(errors).toEqual([]);
    } finally {
      page.off("console", onConsole);
      page.off("pageerror", onPageError);
      await harness.cleanup();
      await resetResearchContinuity("closed");
    }
  },
});
