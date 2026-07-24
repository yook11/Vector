import { randomUUID } from "node:crypto";
import {
  expect,
  type Locator,
  type Page,
  type Route,
  test,
} from "@playwright/test";
import {
  RESEARCH_CONTINUITY,
  RESEARCH_EXPANDED_HISTORY_LIMIT,
  RESEARCH_HISTORY_LIMIT,
  RESEARCH_SOURCE_COUNT,
  RESEARCH_SOURCE_HREF,
  RESEARCH_THREADS,
  type ResearchContinuityFixture,
  type ResearchContinuityVariant,
} from "./fixtures/research";
import {
  installResearchContinuityBrowserHarness,
  type ResearchContinuityBrowserHarness,
  type ResearchContinuityPaintSample,
  type ResearchContinuityRect,
} from "./fixtures/research-continuity";
import {
  completeResearchContinuity,
  failResearchContinuity,
  failResearchSubmission,
  resetResearchContinuity,
  resetResearchDailyQuota,
  resetResearchRateLimits,
} from "./fixtures/research-runtime";

test.beforeEach(async () => {
  await resetResearchRateLimits();
});

const REQUIRED_VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 767, height: 900 },
  { width: 768, height: 900 },
  { width: 1023, height: 900 },
  { width: 1024, height: 768 },
  { width: 1440, height: 900 },
] as const;

interface NewSubmissionRect {
  width: number;
  height: number;
}

interface NewSubmissionPaintSample {
  timestamp: number;
  documentToken: string;
  paperConnected: boolean;
  mastheadConnected: boolean;
  mainConnected: boolean;
  workspaceConnected: boolean;
  composerConnected: boolean;
  samePaper: boolean;
  sameMasthead: boolean;
  sameMain: boolean;
  sameWorkspace: boolean;
  sameComposer: boolean;
  paperRect: NewSubmissionRect;
  mastheadRect: NewSubmissionRect;
  mainRect: NewSubmissionRect;
  workspaceRect: NewSubmissionRect;
  composerRect: NewSubmissionRect;
  composerBusy: string | null;
  submissionStatusCount: number;
  researchSkeletonCount: number;
  pageNavigationOverlayCount: number;
  researchNavigationOverlayCount: number;
  bootstrapLoadingCount: number;
  newsLoadingCount: number;
}

interface NewSubmissionSamplerBridge {
  documentToken: string;
  samples: NewSubmissionPaintSample[];
  stop: () => void;
}

type NewSubmissionWindow = Window & {
  __researchNewSubmissionSampler?: NewSubmissionSamplerBridge;
};

function alphaPath(limit = RESEARCH_HISTORY_LIMIT, legacyView = false) {
  const view = legacyView ? "&view=sources" : "";
  return `/research/${RESEARCH_THREADS.A.id}?limit=${limit}${view}`;
}

function answerPanel(page: Page): Locator {
  return page.locator("[data-research-answer-scroll-region]");
}

function answerRail(page: Page): Locator {
  return page
    .getByTestId("research-answer-slot")
    .first()
    .locator(
      "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' max-w-[860px] ')][1]",
    );
}

function composer(page: Page): Locator {
  return page.locator("form:has(textarea#research-question):visible");
}

function composerRail(page: Page): Locator {
  return composer(page).locator(
    "xpath=./div[contains(concat(' ', normalize-space(@class), ' '), ' max-w-[860px] ')]",
  );
}

async function requiredBox(locator: Locator) {
  await expect(locator).toBeVisible();
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box as NonNullable<typeof box>;
}

function expectSameBox(
  actual: Awaited<ReturnType<typeof requiredBox>>,
  expected: Awaited<ReturnType<typeof requiredBox>>,
) {
  for (const edge of ["x", "y", "width", "height"] as const) {
    expect(Math.abs(actual[edge] - expected[edge])).toBeLessThanOrEqual(1);
  }
}

async function expectScrollable(locator: Locator) {
  await expect
    .poll(() =>
      locator.evaluate(
        (element) => element.scrollHeight > element.clientHeight,
      ),
    )
    .toBe(true);
}

async function scrollByWheel(page: Page, locator: Locator) {
  await expectScrollable(locator);
  await locator.evaluate((element) => {
    element.scrollTop = 0;
  });
  await locator.hover();
  await page.mouse.wheel(0, 700);
  await expect
    .poll(() => locator.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(0);
}

async function expectResearchHrefsWithoutView(page: Page) {
  const links = page.locator('#research-history a[href^="/research"]');
  await expect.poll(() => links.count()).toBeGreaterThan(2);
  const hrefs = await links.evaluateAll((anchors) =>
    anchors.map((anchor) => anchor.getAttribute("href")),
  );
  expect(hrefs.every((href) => href !== null && !href.includes("view="))).toBe(
    true,
  );
}

function continuityPath(fixture: ResearchContinuityFixture): string {
  return `/research/${fixture.threadId}`;
}

function sourceTrigger(page: Page): Locator {
  return page.locator('button[aria-expanded]:has-text("ソース"):visible');
}

function collectPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

async function expectSourcesClosed(page: Page): Promise<void> {
  const trigger = sourceTrigger(page);
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(trigger).not.toHaveAttribute("aria-controls");
  await expect(page.getByRole("complementary", { name: "ソース" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("dialog", { name: "ソース" })).toHaveCount(0);
}

async function installNewSubmissionSampler(
  page: Page,
): Promise<NewSubmissionPaintSample> {
  return page.evaluate(() => {
    const main = document.querySelector<HTMLElement>("main");
    const masthead = document.querySelector<HTMLElement>("header");
    const textarea =
      document.querySelector<HTMLTextAreaElement>("#research-question");
    const composer = textarea?.closest<HTMLFormElement>("form") ?? null;
    const workspace = composer?.closest<HTMLElement>("section") ?? null;
    const paper =
      masthead?.parentElement instanceof HTMLElement
        ? masthead.parentElement
        : null;
    if (
      paper === null ||
      masthead === null ||
      main === null ||
      workspace === null ||
      composer === null
    ) {
      throw new Error("New submission sampler target is missing");
    }
    const initialPaper = paper;
    const initialMasthead = masthead;
    const initialMain = main;
    const initialWorkspace = workspace;
    const initialComposer = composer;

    const documentToken = crypto.randomUUID();
    const samples: NewSubmissionPaintSample[] = [];
    let stopped = false;
    let animationFrame = 0;

    function rect(element: Element): NewSubmissionRect {
      const box = element.getBoundingClientRect();
      return { width: box.width, height: box.height };
    }

    function isVisible(element: Element): boolean {
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return (
        box.width > 0 &&
        box.height > 0 &&
        style.display !== "none" &&
        style.visibility !== "hidden"
      );
    }

    function visibleCount(selector: string): number {
      return Array.from(document.querySelectorAll(selector)).filter(isVisible)
        .length;
    }

    function visibleTextCount(selector: string, text: string): number {
      return Array.from(document.querySelectorAll(selector)).filter(
        (element) =>
          isVisible(element) && element.textContent?.includes(text) === true,
      ).length;
    }

    function currentTargets() {
      const currentMain = document.querySelector<HTMLElement>("main");
      const currentTextarea =
        document.querySelector<HTMLTextAreaElement>("#research-question");
      const currentComposer =
        currentTextarea?.closest<HTMLFormElement>("form") ?? null;
      const currentWorkspace =
        currentComposer?.closest<HTMLElement>("section") ?? null;
      const currentMasthead = document.querySelector<HTMLElement>("header");
      const currentPaper =
        currentMasthead?.parentElement instanceof HTMLElement
          ? currentMasthead.parentElement
          : null;
      return {
        currentPaper,
        currentMasthead,
        currentMain,
        currentWorkspace,
        currentComposer,
      };
    }

    function record(): void {
      const {
        currentPaper,
        currentMasthead,
        currentMain,
        currentWorkspace,
        currentComposer,
      } = currentTargets();
      samples.push({
        timestamp: performance.now(),
        documentToken,
        paperConnected: initialPaper.isConnected,
        mastheadConnected: initialMasthead.isConnected,
        mainConnected: initialMain.isConnected,
        workspaceConnected: initialWorkspace.isConnected,
        composerConnected: initialComposer.isConnected,
        samePaper: currentPaper === initialPaper,
        sameMasthead: currentMasthead === initialMasthead,
        sameMain: currentMain === initialMain,
        sameWorkspace: currentWorkspace === initialWorkspace,
        sameComposer: currentComposer === initialComposer,
        paperRect: rect(initialPaper),
        mastheadRect: rect(initialMasthead),
        mainRect: rect(initialMain),
        workspaceRect: rect(initialWorkspace),
        composerRect: rect(initialComposer),
        composerBusy: currentComposer?.getAttribute("aria-busy") ?? null,
        submissionStatusCount: visibleCount(
          '[role="status"][aria-label="質問を送信しています…"]',
        ),
        researchSkeletonCount: visibleCount(
          '[role="status"][aria-label="Researchを読み込み中…"]',
        ),
        pageNavigationOverlayCount: visibleCount(
          '[data-testid="page-navigation-overlay"]',
        ),
        researchNavigationOverlayCount: visibleCount(
          '[data-testid="research-navigation-overlay"]',
        ),
        bootstrapLoadingCount: visibleTextCount(
          '[role="status"]',
          "画面を準備しています…",
        ),
        newsLoadingCount: visibleTextCount(
          '[role="status"]',
          "記事を読み込み中…",
        ),
      });
    }

    function sampleNextPaint(): void {
      if (stopped) return;
      animationFrame = requestAnimationFrame(() => {
        record();
        sampleNextPaint();
      });
    }

    const bridge: NewSubmissionSamplerBridge = {
      documentToken,
      samples,
      stop: () => {
        stopped = true;
        cancelAnimationFrame(animationFrame);
      },
    };
    (window as NewSubmissionWindow).__researchNewSubmissionSampler = bridge;
    record();
    sampleNextPaint();
    return samples[0] as NewSubmissionPaintSample;
  });
}

async function newSubmissionSamples(
  page: Page,
): Promise<NewSubmissionPaintSample[]> {
  return page.evaluate(() => {
    const bridge = (window as NewSubmissionWindow)
      .__researchNewSubmissionSampler;
    if (bridge === undefined) {
      throw new Error("New submission sampler document was replaced");
    }
    return bridge.samples;
  });
}

async function stopNewSubmissionSampler(page: Page): Promise<void> {
  await page.evaluate(() => {
    const bridge = (window as NewSubmissionWindow)
      .__researchNewSubmissionSampler;
    if (bridge === undefined) {
      throw new Error("New submission sampler document was replaced");
    }
    bridge.stop();
  });
}

function expectNewSubmissionSamples(
  samples: readonly NewSubmissionPaintSample[],
  baseline: NewSubmissionPaintSample,
): void {
  expect(samples.length).toBeGreaterThanOrEqual(3);
  expect(
    samples.some(
      (sample) =>
        sample.composerBusy === "true" && sample.submissionStatusCount === 1,
    ),
  ).toBe(true);
  expect(samples.at(-1)?.composerBusy).toBe("false");
  expect(samples.at(-1)?.submissionStatusCount).toBe(0);

  const persistentTargets = [
    {
      label: "paper",
      valid: (sample: NewSubmissionPaintSample) =>
        sample.paperConnected &&
        sample.samePaper &&
        sample.paperRect.width > 0 &&
        sample.paperRect.height > 0,
    },
    {
      label: "masthead",
      valid: (sample: NewSubmissionPaintSample) =>
        sample.mastheadConnected &&
        sample.sameMasthead &&
        sample.mastheadRect.width > 0 &&
        sample.mastheadRect.height > 0,
    },
    {
      label: "main",
      valid: (sample: NewSubmissionPaintSample) =>
        sample.mainConnected &&
        sample.sameMain &&
        sample.mainRect.width > 0 &&
        sample.mainRect.height > 0,
    },
    {
      label: "workspace",
      valid: (sample: NewSubmissionPaintSample) =>
        sample.workspaceConnected &&
        sample.sameWorkspace &&
        sample.workspaceRect.width > 0 &&
        sample.workspaceRect.height > 0,
    },
    {
      label: "composer",
      valid: (sample: NewSubmissionPaintSample) =>
        sample.composerConnected &&
        sample.sameComposer &&
        sample.composerRect.width > 0 &&
        sample.composerRect.height > 0,
    },
  ] as const;
  for (const target of persistentTargets) {
    const failingIndex = samples.findIndex((sample) => !target.valid(sample));
    const failedSample =
      failingIndex === -1 ? undefined : samples[failingIndex];
    expect
      .soft(
        failingIndex,
        `${target.label} persistence first failed sample: ${JSON.stringify(failedSample)}`,
      )
      .toBe(-1);
  }
  expect
    .soft(
      samples.findIndex(
        (sample) => sample.documentToken !== baseline.documentToken,
      ),
      "document token changed",
    )
    .toBe(-1);
  expect
    .soft(
      samples.findIndex((sample) => sample.submissionStatusCount > 1),
      "submission status duplicated",
    )
    .toBe(-1);
  expect
    .soft(
      samples.findIndex(
        (sample) =>
          sample.researchSkeletonCount > 0 ||
          sample.pageNavigationOverlayCount > 0 ||
          sample.researchNavigationOverlayCount > 0 ||
          sample.bootstrapLoadingCount > 0 ||
          sample.newsLoadingCount > 0,
      ),
      "protected fallback or global overlay appeared",
    )
    .toBe(-1);
}

async function deleteCurrentResearchThreadThroughUi(page: Page): Promise<void> {
  const pathname = new URL(page.url()).pathname;
  if (!/^\/research\/[0-9a-f-]{36}$/.test(pathname)) return;

  const deleteButton = page.getByRole("button", {
    name: "スレッドを削除",
  });
  await expect(deleteButton).toBeVisible();
  await deleteButton.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  const deleteResponse = page.waitForResponse(
    (response) => {
      const request = response.request();
      return (
        request.method() === "POST" &&
        new URL(request.url()).pathname === pathname &&
        request.headers()["next-action"] !== undefined
      );
    },
    { timeout: 30_000 },
  );
  await dialog.getByRole("button", { name: "削除", exact: true }).click();
  expect((await deleteResponse).status()).toBeLessThan(400);
  await page.goto("/research");
  await expect(
    page.getByRole("heading", { name: "新しいリサーチ" }),
  ).toBeVisible();
  await expect(page.locator(`a[href="${pathname}"]`)).toHaveCount(0);
}

type AcceptedResearchTarget = {
  path: string;
  runId: string;
  threadId: string;
};

function acceptedResearchTargetFromActionResponse(
  body: string,
): AcceptedResearchTarget {
  const threadMatch = body.match(
    /"threadId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"/i,
  );
  const runMatch = body.match(
    /"runId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"/i,
  );
  if (threadMatch?.[1] === undefined || runMatch?.[1] === undefined) {
    throw new Error(
      "Accepted Research action response is missing committed run identity",
    );
  }
  return {
    path: `/research/${threadMatch[1]}`,
    runId: runMatch[1],
    threadId: threadMatch[1],
  };
}

async function setDistanceFromAnswerBottom(
  scroller: Locator,
  distance: number,
): Promise<{ scrollTop: number; scrollHeight: number; clientHeight: number }> {
  return scroller.evaluate((element, targetDistance) => {
    const maxScrollTop = element.scrollHeight - element.clientHeight;
    if (maxScrollTop <= targetDistance) {
      throw new Error("Answer fixture is not tall enough for continuity probe");
    }
    element.scrollTop = maxScrollTop - targetDistance;
    element.dispatchEvent(new Event("scroll"));
    return {
      scrollTop: element.scrollTop,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
    };
  }, distance);
}

function expectRectWithin(
  actual: ResearchContinuityRect,
  expected: ResearchContinuityRect,
  edges: readonly (keyof ResearchContinuityRect)[],
  label: string,
): void {
  for (const edge of edges) {
    expect
      .soft(Math.abs(actual[edge] - expected[edge]), `${label}.${edge}`)
      .toBeLessThanOrEqual(1);
  }
}

function expectContinuitySamples(
  samples: readonly ResearchContinuityPaintSample[],
  baseline: ResearchContinuityPaintSample,
  expectedSurface: "closed" | "inline",
): void {
  expect(samples.length).toBeGreaterThanOrEqual(3);
  expect(
    samples.some(
      (sample) =>
        sample.persistedStatus === "running" && sample.failureCount === 1,
    ),
  ).toBe(true);
  expect(samples.at(-1)?.persistedStatus).toBe("failed");
  const firstFailureSample = samples.find(
    (sample) => sample.failureCount === 1,
  );
  expect(firstFailureSample).toBeDefined();

  for (const [index, sample] of samples.entries()) {
    expect
      .soft(sample.draftCount + sample.failureCount, `sample ${index}`)
      .toBe(1);
    expect.soft(sample.failureCount, `failure ${index}`).toBeLessThanOrEqual(1);
    expect.soft(sample.protectedLoadingCount, `loading ${index}`).toBe(0);
    expect.soft(sample.announcerCount, `announcer ${index}`).toBe(1);
    expect.soft(sample.sameTurn, `turn identity ${index}`).toBe(true);
    expect.soft(sample.sameThreadPanel, `main identity ${index}`).toBe(true);
    expect.soft(sample.sameComposer, `composer identity ${index}`).toBe(true);
    expect
      .soft(sample.sameAnswerScroller, `scroller identity ${index}`)
      .toBe(true);
    expect.soft(sample.sameFocus, `focus ${index}`).toBe(true);
    expect
      .soft(sample.documentToken, `document ${index}`)
      .toBe(baseline.documentToken);
    if (sample.failureCount === 1) {
      expect
        .soft(sample.sameFailureRail, `failure identity ${index}`)
        .toBe(true);
    }
    expectRectWithin(
      sample.turnRect,
      baseline.turnRect,
      ["x", "width"],
      `turn ${index}`,
    );
    if (sample.failureCount === 1 && firstFailureSample !== undefined) {
      expectRectWithin(
        sample.turnRect,
        firstFailureSample.turnRect,
        ["x", "y", "width"],
        `failed turn ${index}`,
      );
    }
    expectRectWithin(
      sample.threadPanelRect,
      baseline.threadPanelRect,
      ["x", "y", "width", "height"],
      `main ${index}`,
    );
    expectRectWithin(
      sample.composerRect,
      baseline.composerRect,
      ["x", "y", "width", "height"],
      `composer ${index}`,
    );

    if (expectedSurface === "closed") {
      expect.soft(sample.sourceSurfaceCount, `surface ${index}`).toBe(0);
      expect.soft(sample.sourcesExpanded, `expanded ${index}`).toBe("false");
      expect.soft(sample.sourcesControls, `controls ${index}`).toBeNull();
    } else {
      expect.soft(sample.sourceSurfaceCount, `surface ${index}`).toBe(1);
      expect.soft(sample.sourcesExpanded, `expanded ${index}`).toBe("true");
      expect
        .soft(sample.sourcesControls, `controls ${index}`)
        .toBe("research-sources-inline");
      expect
        .soft(sample.sameSourceScroller, `source identity ${index}`)
        .toBe(true);
      expect
        .soft(
          Math.abs(
            (sample.sourceScrollTop ?? 0) - (baseline.sourceScrollTop ?? 0),
          ),
          `source scroll ${index}`,
        )
        .toBeLessThanOrEqual(1);
      expect
        .soft(sample.focusedHref, `source focus ${index}`)
        .toBe(baseline.focusedHref);
    }
  }

  const finalSample = samples.at(-1);
  expect(finalSample).toBeDefined();
  if (finalSample === undefined) return;
  const expectedScrollTop = Math.min(
    baseline.answerScrollTop,
    Math.max(
      0,
      finalSample.answerScrollHeight - finalSample.answerClientHeight,
    ),
  );
  expect(
    Math.abs(finalSample.answerScrollTop - expectedScrollTop),
  ).toBeLessThanOrEqual(1);
}

function expectCompletedContinuitySamples(
  samples: readonly ResearchContinuityPaintSample[],
  baseline: ResearchContinuityPaintSample,
): void {
  expect(samples.length).toBeGreaterThanOrEqual(3);
  expect(samples.some((sample) => sample.persistedStatus === "running")).toBe(
    true,
  );
  expect(samples.at(-1)?.persistedStatus).toBe("completed");

  for (const [index, sample] of samples.entries()) {
    expect.soft(sample.draftCount, `slot ${index}`).toBe(1);
    expect.soft(sample.failureCount, `failure ${index}`).toBe(0);
    expect.soft(sample.protectedLoadingCount, `loading ${index}`).toBe(0);
    expect.soft(sample.announcerCount, `announcer ${index}`).toBe(1);
    expect.soft(sample.sameTurn, `turn identity ${index}`).toBe(true);
    expect.soft(sample.sameThreadPanel, `main identity ${index}`).toBe(true);
    expect.soft(sample.sameComposer, `composer identity ${index}`).toBe(true);
    expect
      .soft(sample.sameAnswerScroller, `scroller identity ${index}`)
      .toBe(true);
    expect.soft(sample.sameAnswerSlot, `slot identity ${index}`).toBe(true);
    expect
      .soft(sample.documentToken, `document ${index}`)
      .toBe(baseline.documentToken);
    expectRectWithin(
      sample.turnRect,
      baseline.turnRect,
      ["x", "width"],
      `turn ${index}`,
    );
    expectRectWithin(
      sample.threadPanelRect,
      baseline.threadPanelRect,
      ["x", "y", "width", "height"],
      `main ${index}`,
    );
    expectRectWithin(
      sample.composerRect,
      baseline.composerRect,
      ["x", "y", "width", "height"],
      `composer ${index}`,
    );
  }
}

async function runFailedTerminalContinuity({
  page,
  variant,
  harness,
  expectedSurface,
}: {
  page: Page;
  variant: ResearchContinuityVariant;
  harness: ResearchContinuityBrowserHarness;
  expectedSurface: "closed" | "inline";
}): Promise<{
  baseline: ResearchContinuityPaintSample;
  samples: ResearchContinuityPaintSample[];
}> {
  const fixture = RESEARCH_CONTINUITY[variant];
  const turn = page.locator(`[data-research-run-id="${fixture.activeRunId}"]`);
  const failure = turn.locator("[data-research-failure-rail]");
  await harness.emitDraft();
  await expect(
    turn.getByText("E2E continuity live draft marker 1", { exact: false }),
  ).toBeVisible();
  await setDistanceFromAnswerBottom(answerPanel(page), 97);
  const baseline = await harness.startSampler();
  harness.armTerminalRefreshGate();

  await expect
    .poll(async () => (await harness.stats()).targetPollResponses)
    .toBeGreaterThan(0);
  const pollResponsesBeforeFailure = (await harness.stats())
    .targetPollResponses;
  await failResearchContinuity(variant);
  await expect
    .poll(async () => (await harness.stats()).targetPollResponses, {
      timeout: 5_000,
    })
    .toBeGreaterThan(pollResponsesBeforeFailure);
  await harness.emitFailedTerminal();

  await expect(failure).toHaveCount(1);
  await expect(turn.getByTestId("research-answer-slot")).toHaveCount(0);
  await expect
    .poll(async () => (await harness.stats()).eventSourcesClosed)
    .toBe(1);
  await harness.waitForTerminalRefresh();
  await page.waitForTimeout(3_100);

  const heldStats = await harness.stats();
  expect(heldStats.terminalRscRequests).toBe(1);
  expect(heldStats.targetPollStatuses).not.toContain("failed");
  expect(heldStats.eventSourcesCreated).toBe(1);
  expect(heldStats.eventSourcesClosed).toBe(1);
  expect(heldStats.draftEventsSent).toBe(1);
  expect(heldStats.terminalEventsSent).toBe(1);
  expect(heldStats.terminalMainFrameNavigations).toBe(0);
  await expect(turn).toHaveAttribute(
    "data-research-persisted-status",
    "running",
  );
  await expect(failure).toHaveCount(1);
  await expect(
    page.locator('[data-testid="research-navigation-overlay"]:visible'),
  ).toHaveCount(0);

  harness.releaseTerminalRefresh();
  await harness.waitForPersistedSample();
  await expect(turn).toHaveAttribute(
    "data-research-persisted-status",
    "failed",
  );
  const samples = await harness.samples();
  const finalStats = await harness.stats();
  expect(finalStats.terminalRscRequests).toBe(1);
  expect(finalStats.terminalMainFrameNavigations).toBe(0);
  expect(finalStats.documentToken).toBe(baseline.documentToken);
  expect(
    finalStats.targetPollStatuses.every((status) => status === "running"),
  ).toBe(true);
  expectContinuitySamples(samples, baseline, expectedSurface);
  return { baseline, samples };
}

test("new research submitはServer Action response commitとclient navigation中も同じworkspaceを維持する", async ({
  page,
}) => {
  test.slow();
  const errors = collectPageErrors(page);
  const actionRelease = Promise.withResolvers<void>();
  const upstreamCompleted = Promise.withResolvers<void>();
  const handlerSettled = Promise.withResolvers<void>();
  const actionPattern = "**/research";
  const question = `E2E action response continuity ${randomUUID()}`;
  let actionRequests = 0;
  let actionWasFetched = false;
  let acceptedPath: string | null = null;
  let samplerStarted = false;
  let samplerStopped = false;

  const actionHandler = async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (
      request.method() !== "POST" ||
      url.pathname !== "/research" ||
      request.headers()["next-action"] === undefined
    ) {
      await route.continue();
      return;
    }

    actionRequests += 1;
    try {
      const response = await route.fetch({
        maxRedirects: 0,
        timeout: 30_000,
      });
      const responseBody = await response.body();
      actionWasFetched = true;
      acceptedPath = acceptedResearchTargetFromActionResponse(
        responseBody.toString("utf8"),
      ).path;
      upstreamCompleted.resolve();
      await actionRelease.promise;
      await route.fulfill({ response, body: responseBody });
      handlerSettled.resolve();
    } catch (error) {
      upstreamCompleted.reject(error);
      handlerSettled.reject(error);
      await route.abort("failed");
    }
  };

  await page.setViewportSize({ width: 1440, height: 900 });
  await resetResearchDailyQuota();
  await page.goto("/research");
  await expect(
    page.getByRole("heading", { name: "新しいリサーチ" }),
  ).toBeVisible();
  await page.route(actionPattern, actionHandler);

  try {
    const main = page.getByRole("main");
    const form = composer(page);
    const workspace = form.locator("xpath=ancestor::section[1]");
    const masthead = page.getByRole("banner");
    const paperSurface = masthead.locator("xpath=..");
    await expect(paperSurface).toBeVisible();
    await expect(masthead).toBeVisible();
    await expect(main).toBeVisible();
    await expect(workspace).toBeVisible();
    await expect(form).toBeVisible();

    const baseline = await installNewSubmissionSampler(page);
    samplerStarted = true;

    await page.getByRole("textbox", { name: "質問" }).fill(question);
    await page.getByRole("button", { name: "送信", exact: true }).click();
    await upstreamCompleted.promise;

    const submissionStatus = page.locator(
      '[role="status"][aria-label="質問を送信しています…"]:visible',
    );
    expect(actionRequests).toBe(1);
    await expect(main).toHaveAttribute("aria-busy", "true");
    await expect(form).toHaveAttribute("aria-busy", "true");
    await expect(page.getByRole("button", { name: "送信中…" })).toBeDisabled();
    await expect(submissionStatus).toHaveCount(1);
    await expect(paperSurface).toBeVisible();
    await expect(masthead).toBeVisible();
    await expect(main).toBeVisible();
    await expect(workspace).toBeVisible();
    await expect(form).toBeVisible();
    await expect(
      page.locator('[data-testid="page-navigation-overlay"]:visible'),
    ).toHaveCount(0);
    await expect(
      page.locator('[data-testid="research-navigation-overlay"]:visible'),
    ).toHaveCount(0);
    await expect(
      page.locator(
        '[role="status"][aria-label="Researchを読み込み中…"]:visible',
      ),
    ).toHaveCount(0);
    await expect(
      page.getByText("画面を準備しています…", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("記事を読み込み中…", { exact: true }),
    ).toHaveCount(0);

    await expect
      .poll(async () =>
        (await newSubmissionSamples(page)).some(
          (sample) =>
            sample.composerBusy === "true" &&
            sample.submissionStatusCount === 1,
        ),
      )
      .toBe(true);

    actionRelease.resolve();
    await handlerSettled.promise;
    const expectedThreadPath = acceptedPath;
    if (expectedThreadPath === null) {
      throw new Error("Accepted Research path was not captured");
    }
    await expect
      .poll(() => new URL(page.url()).pathname)
      .toBe(expectedThreadPath);
    const activeTurn = page.locator(
      '[data-research-run-id][data-research-persisted-status="queued"], [data-research-run-id][data-research-persisted-status="running"]',
    );
    await expect(activeTurn).toHaveCount(1);
    await expect(page.getByRole("button", { name: "停止" })).toBeVisible();
    await expect(main).toHaveAttribute("aria-busy", "false");
    await expect(form).toHaveAttribute("aria-busy", "false");
    await expect(submissionStatus).toHaveCount(0);
    await expect(paperSurface).toBeVisible();
    await expect(masthead).toBeVisible();
    await expect(main).toBeVisible();
    await expect(workspace).toBeVisible();
    await expect(form).toBeVisible();

    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => resolve());
        }),
    );
    await stopNewSubmissionSampler(page);
    samplerStopped = true;
    expectNewSubmissionSamples(await newSubmissionSamples(page), baseline);
    expect(errors).toEqual([]);
  } finally {
    actionRelease.resolve();
    if (actionWasFetched) {
      await handlerSettled.promise.catch(() => undefined);
      if (
        acceptedPath !== null &&
        new URL(page.url()).pathname !== acceptedPath
      ) {
        await page.goto(acceptedPath);
      }
      if (acceptedPath !== null) {
        await deleteCurrentResearchThreadThroughUi(page);
      }
    }
    if (samplerStarted && !samplerStopped) {
      await stopNewSubmissionSampler(page).catch(() => undefined);
    }
    await page.unroute(actionPattern, actionHandler);
  }
});

test("new research submitのfirst committed modelがfailedでもsubmission lockを解除する", async ({
  page,
}) => {
  test.slow();
  const errors = collectPageErrors(page);
  const actionRelease = Promise.withResolvers<void>();
  const acceptedTarget = Promise.withResolvers<AcceptedResearchTarget>();
  const handlerSettled = Promise.withResolvers<void>();
  const actionPattern = "**/research";
  const question = `E2E first model terminal ${randomUUID()}`;
  let createdTarget: AcceptedResearchTarget | null = null;
  let actionWasFetched = false;
  let samplerStarted = false;
  let samplerStopped = false;

  const actionHandler = async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (
      request.method() !== "POST" ||
      url.pathname !== "/research" ||
      request.headers()["next-action"] === undefined
    ) {
      await route.continue();
      return;
    }

    try {
      const response = await route.fetch({
        maxRedirects: 0,
        timeout: 30_000,
      });
      const responseBody = await response.body();
      actionWasFetched = true;
      createdTarget = acceptedResearchTargetFromActionResponse(
        responseBody.toString("utf8"),
      );
      acceptedTarget.resolve(createdTarget);
      await actionRelease.promise;
      await route.fulfill({ response, body: responseBody });
      handlerSettled.resolve();
    } catch (error) {
      acceptedTarget.reject(error);
      handlerSettled.reject(error);
      await route.abort("failed");
    }
  };

  await page.setViewportSize({ width: 1440, height: 900 });
  await resetResearchDailyQuota();
  await page.goto("/research");
  await expect(
    page.getByRole("heading", { name: "新しいリサーチ" }),
  ).toBeVisible();
  await page.route(actionPattern, actionHandler);

  try {
    const main = page.getByRole("main");
    const form = composer(page);
    const workspace = form.locator("xpath=ancestor::section[1]");
    const masthead = page.getByRole("banner");
    const paperSurface = masthead.locator("xpath=..");
    const baseline = await installNewSubmissionSampler(page);
    samplerStarted = true;

    await page.getByRole("textbox", { name: "質問" }).fill(question);
    await page.getByRole("button", { name: "送信", exact: true }).click();
    const target = await acceptedTarget.promise;
    createdTarget = target;

    await expect(main).toHaveAttribute("aria-busy", "true");
    await expect(form).toHaveAttribute("aria-busy", "true");
    await expect(
      page.locator(
        '[role="status"][aria-label="質問を送信しています…"]:visible',
      ),
    ).toHaveCount(1);

    await failResearchSubmission(target.runId);
    actionRelease.resolve();
    await handlerSettled.promise;
    await expect.poll(() => new URL(page.url()).pathname).toBe(target.path);

    const failedTurn = page.locator(
      `[data-research-run-id="${target.runId}"][data-research-persisted-status="failed"]`,
    );
    await expect(failedTurn).toHaveCount(1);
    await expect(
      failedTurn.locator("[data-research-failure-rail]"),
    ).toHaveCount(1);
    await expect(main).toHaveAttribute("aria-busy", "false");
    await expect(form).toHaveAttribute("aria-busy", "false");
    await expect(
      page.locator(
        '[role="status"][aria-label="質問を送信しています…"]:visible',
      ),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "スレッドを削除" }),
    ).toBeEnabled();
    await expect(
      page.getByRole("link", { name: "新しいスレッド" }),
    ).not.toHaveAttribute("aria-disabled", "true");
    await expect(paperSurface).toBeVisible();
    await expect(masthead).toBeVisible();
    await expect(main).toBeVisible();
    await expect(workspace).toBeVisible();
    await expect(form).toBeVisible();
    await expect(
      page.locator('[data-testid="page-navigation-overlay"]:visible'),
    ).toHaveCount(0);
    await expect(
      page.locator('[data-testid="research-navigation-overlay"]:visible'),
    ).toHaveCount(0);
    await expect(
      page.locator(
        '[role="status"][aria-label="Researchを読み込み中…"]:visible',
      ),
    ).toHaveCount(0);

    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => resolve());
        }),
    );
    await stopNewSubmissionSampler(page);
    samplerStopped = true;
    expectNewSubmissionSamples(await newSubmissionSamples(page), baseline);
    expect(errors).toEqual([]);
  } finally {
    actionRelease.resolve();
    if (actionWasFetched) {
      await handlerSettled.promise.catch(() => undefined);
      if (
        createdTarget !== null &&
        new URL(page.url()).pathname !== createdTarget.path
      ) {
        await page.goto(createdTarget.path);
      }
      if (createdTarget !== null) {
        await deleteCurrentResearchThreadThroughUi(page);
      }
    }
    if (samplerStarted && !samplerStopped) {
      await stopNewSubmissionSampler(page).catch(() => undefined);
    }
    await page.unroute(actionPattern, actionHandler);
  }
});

test("closed fixtureはnavigationからterminal RSCまで全幅とfailureを維持する", async ({
  page,
}) => {
  test.slow();
  const fixture = RESEARCH_CONTINUITY.closed;
  const errors = collectPageErrors(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await resetResearchContinuity("closed");
  const harness = await installResearchContinuityBrowserHarness(page, fixture);

  try {
    await page.goto("/research");
    await page.getByRole("link", { name: new RegExp(fixture.title) }).click();
    await expect(page).toHaveURL(new RegExp(fixture.threadId));
    await expect(
      page.getByRole("heading", { name: fixture.title }),
    ).toBeVisible();
    await expectSourcesClosed(page);
    const answerBox = await requiredBox(answerPanel(page));
    const composerBox = await requiredBox(composer(page));
    expect(Math.abs(answerBox.width - composerBox.width)).toBeLessThanOrEqual(
      1,
    );
    await sourceTrigger(page).focus();
    await expect(sourceTrigger(page)).toBeFocused();

    await runFailedTerminalContinuity({
      page,
      variant: "closed",
      harness,
      expectedSurface: "closed",
    });
    await expectSourcesClosed(page);
  } finally {
    await harness.cleanup();
  }

  expect((await harness.stats()).terminalRscRequests).toBe(1);
  expect(errors).toEqual([]);
});

test("open fixtureはexplicit inline stateをterminal RSC後も維持する", async ({
  page,
}) => {
  test.slow();
  const fixture = RESEARCH_CONTINUITY.open;
  const errors = collectPageErrors(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await resetResearchContinuity("open");
  const harness = await installResearchContinuityBrowserHarness(page, fixture);

  try {
    await page.goto(continuityPath(fixture));
    await expect(
      page.getByRole("heading", { name: fixture.title }),
    ).toBeVisible();
    await expectSourcesClosed(page);
    const answerBeforeOpen = await requiredBox(answerPanel(page));
    const composerBeforeOpen = await requiredBox(composer(page));

    const trigger = sourceTrigger(page);
    await trigger.click();
    const inline = page.getByRole("complementary", { name: "ソース" });
    await expect(inline).toBeVisible();
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
    await expect(trigger).toHaveAttribute(
      "aria-controls",
      "research-sources-inline",
    );
    const answerAfterOpen = await requiredBox(answerPanel(page));
    const composerAfterOpen = await requiredBox(composer(page));
    expect(
      Math.abs(answerBeforeOpen.width - answerAfterOpen.width - 320),
    ).toBeLessThanOrEqual(1);
    expect(
      Math.abs(composerBeforeOpen.width - composerAfterOpen.width - 320),
    ).toBeLessThanOrEqual(1);

    const sourceScroller = inline.locator(".overflow-y-auto");
    await expectScrollable(sourceScroller);
    const sourceLink = inline.locator(`a[href="${fixture.sourceHref}"]`);
    await sourceLink.focus();
    await expect(sourceLink).toBeFocused();
    await sourceScroller.evaluate((element) => {
      element.scrollTop = 77;
    });
    await expect
      .poll(() => sourceScroller.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(0);
    await expect(sourceLink).toBeFocused();

    const { baseline } = await runFailedTerminalContinuity({
      page,
      variant: "open",
      harness,
      expectedSurface: "inline",
    });
    await expect(inline).toBeVisible();
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
    await expect(trigger).toHaveAttribute(
      "aria-controls",
      "research-sources-inline",
    );
    expect(
      Math.abs(
        (await sourceScroller.evaluate((element) => element.scrollTop)) -
          (baseline.sourceScrollTop ?? 0),
      ),
    ).toBeLessThanOrEqual(1);
    await expect(sourceLink).toBeFocused();
  } finally {
    await harness.cleanup();
  }

  expect((await harness.stats()).terminalRscRequests).toBe(1);
  expect(errors).toEqual([]);
});

test("completed terminalはRSC gate中もdraftとworkspaceを維持しfinalへ同じslotで収束する", async ({
  page,
}) => {
  test.slow();
  const fixture = RESEARCH_CONTINUITY.closed;
  const errors = collectPageErrors(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await resetResearchContinuity("closed");
  const harness = await installResearchContinuityBrowserHarness(page, fixture, {
    terminalStatus: "completed",
  });
  const turn = page.locator(`[data-research-run-id="${fixture.activeRunId}"]`);

  try {
    await page.goto(continuityPath(fixture));
    await expect(
      page.getByRole("heading", { name: fixture.title }),
    ).toBeVisible();
    await harness.emitDraft();
    await expect(
      turn.getByText("E2E continuity live draft marker 1", { exact: false }),
    ).toBeVisible();
    const baseline = await harness.startSampler();
    harness.armTerminalRefreshGate();

    await completeResearchContinuity("closed");
    await harness.emitCompletedTerminal();
    await harness.waitForTerminalRefresh();

    await expect(turn).toHaveAttribute(
      "data-research-persisted-status",
      "running",
    );
    await expect(
      turn.getByText("E2E continuity live draft marker 1", { exact: false }),
    ).toBeVisible();
    await expect(turn.getByText("回答を確定しています…")).toBeVisible();
    await expect(page.getByRole("main")).toBeVisible();
    await expect(composer(page)).toBeVisible();
    await expect(answerPanel(page)).toBeVisible();
    await expect(turn.getByTestId("research-answer-slot")).toHaveCount(1);
    await expect(
      page.getByText("Researchを読み込み中…", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.locator('[data-testid="research-navigation-overlay"]:visible'),
    ).toHaveCount(0);

    const heldStats = await harness.stats();
    expect(heldStats.terminalRscRequests).toBe(1);
    expect(heldStats.terminalMainFrameNavigations).toBe(0);
    expect(heldStats.eventSourcesCreated).toBe(1);
    expect(heldStats.eventSourcesClosed).toBe(1);

    harness.releaseTerminalRefresh();
    await harness.waitForPersistedSample();
    await expect(turn).toHaveAttribute(
      "data-research-persisted-status",
      "completed",
    );
    await expect(
      turn.getByText(fixture.completedActiveAnswerMarker),
    ).toBeVisible();
    await expect(
      turn.getByText("E2E continuity live draft marker 1", { exact: false }),
    ).toHaveCount(0);
    await expect(turn.getByTestId("research-answer-slot")).toHaveCount(1);
    await expect(
      page.locator('[role="status"][aria-live="polite"][aria-atomic="true"]'),
    ).toHaveText("回答が完了しました");

    const samples = await harness.samples();
    expectCompletedContinuitySamples(samples, baseline);
  } finally {
    await harness.cleanup();
  }

  expect((await harness.stats()).terminalRscRequests).toBe(1);
  expect(errors).toEqual([]);
});

test("source 0件のhard loadはdisabledのままsurfaceをmountしない", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/research/${RESEARCH_THREADS.C.id}`);
  await expect(
    page.getByRole("heading", { name: RESEARCH_THREADS.C.title }),
  ).toBeVisible();
  const trigger = sourceTrigger(page);
  await expect(trigger).toBeDisabled();
  await expect(trigger).toContainText("0");
  await expectSourcesClosed(page);
});

test("continuity threadのA→B→Aで過去のsource open stateを復元しない", async ({
  page,
}) => {
  const errors = collectPageErrors(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await resetResearchContinuity("closed");
  await failResearchContinuity("closed");
  await resetResearchContinuity("open");
  await failResearchContinuity("open");

  await page.goto(continuityPath(RESEARCH_CONTINUITY.closed));
  await expectSourcesClosed(page);
  await sourceTrigger(page).click();
  await expect(
    page.getByRole("complementary", { name: "ソース" }),
  ).toBeVisible();

  await page
    .getByRole("link", { name: new RegExp(RESEARCH_CONTINUITY.open.title) })
    .click();
  await expect(page).toHaveURL(new RegExp(RESEARCH_CONTINUITY.open.threadId));
  await expectSourcesClosed(page);
  await sourceTrigger(page).click();
  await expect(
    page.getByRole("complementary", { name: "ソース" }),
  ).toBeVisible();

  await page
    .getByRole("link", { name: new RegExp(RESEARCH_CONTINUITY.closed.title) })
    .click();
  await expect(page).toHaveURL(new RegExp(RESEARCH_CONTINUITY.closed.threadId));
  await expectSourcesClosed(page);
  expect(errors).toEqual([]);
});

test("thread切替中に対象・旧本文・操作lockを表示してcommit後に解除する", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));

  const aPath = `/research/${RESEARCH_THREADS.A.id}?limit=2`;
  const bPath = `/research/${RESEARCH_THREADS.B.id}?limit=2`;
  await page.goto(aPath);

  const main = page.getByRole("main");
  const aLink = page.locator(`a[href="${aPath}"]`);
  const bLink = page.locator(`a[href="${bPath}"]`);
  const moreLink = page.getByRole("link", { name: "さらに表示" });
  const newLink = page.getByRole("link", { name: "新しいスレッド" });
  const textarea = page.getByRole("textbox", { name: "質問" });
  const send = page.getByRole("button", { name: "送信" });
  const deleteButton = page.getByRole("button", { name: "スレッドを削除" });

  await expect(
    page.getByRole("heading", { name: RESEARCH_THREADS.A.title }),
  ).toBeVisible();
  await expect(page.getByText(RESEARCH_THREADS.A.answer)).toBeVisible();
  await expect(aLink).toHaveAttribute("aria-current", "page");
  await expect(moreLink).toBeVisible();
  const moreHref = await moreLink.getAttribute("href");
  expect(moreHref).not.toBeNull();
  const moreUrl = new URL(moreHref ?? "", "http://research.local");
  expect(moreUrl.pathname).toBe(`/research/${RESEARCH_THREADS.A.id}`);
  expect(
    Number(
      moreUrl.searchParams.get("limit") ?? RESEARCH_EXPANDED_HISTORY_LIMIT,
    ),
  ).toBe(RESEARCH_EXPANDED_HISTORY_LIMIT);
  await textarea.fill("pendingでも保持する入力");
  await expect(send).toBeEnabled();

  let releaseResponse!: () => void;
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let signalRequest!: () => void;
  const requestArrived = new Promise<void>((resolve) => {
    signalRequest = resolve;
  });
  let gatedRequests = 0;
  await page.route(`**/research/${RESEARCH_THREADS.B.id}*`, async (route) => {
    if (route.request().headers().rsc !== "1") {
      await route.continue();
      return;
    }
    gatedRequests += 1;
    signalRequest();
    await Promise.all([
      responseGate,
      new Promise((resolve) => setTimeout(resolve, 2_000)),
    ]);
    await route.continue();
  });

  const navigation = bLink.click();
  await requestArrived;

  await expect(bLink).toContainText("読み込み中…");
  await expect(bLink).toHaveAttribute("aria-busy", "true");
  await expect(bLink).toHaveAttribute("aria-disabled", "true");
  await expect(main).toHaveAttribute("aria-busy", "true");
  await expect(page.getByRole("status")).toContainText(
    `「${RESEARCH_THREADS.B.title}」を読み込み中…`,
  );
  await expect(page.getByTestId("research-navigation-overlay")).toContainText(
    `「${RESEARCH_THREADS.B.title}」を読み込み中…`,
  );
  await expect(page.getByText(RESEARCH_THREADS.A.answer)).toBeVisible();
  await expect(textarea).toHaveValue("pendingでも保持する入力");
  await expect(deleteButton).toBeDisabled();
  await expect(textarea).toBeDisabled();
  await expect(send).toBeDisabled();
  await expect(page.getByText("記事を読み込み中")).toHaveCount(0);

  await aLink.click({ force: true });
  await bLink.click({ force: true });
  await newLink.click({ force: true });
  await moreLink.click({ force: true });
  expect(gatedRequests).toBe(1);
  await expect(page).toHaveURL(new RegExp(`${RESEARCH_THREADS.A.id}`));
  await expect(page.getByRole("status")).toContainText(
    RESEARCH_THREADS.B.title,
  );

  releaseResponse();
  await navigation;
  await expect(page).toHaveURL(bPath);
  await expect(
    page.getByRole("heading", { name: RESEARCH_THREADS.B.title }),
  ).toBeVisible();
  await expect(page.getByText(RESEARCH_THREADS.B.answer)).toBeVisible();
  await expect(
    page.getByRole("link", { name: new RegExp(RESEARCH_THREADS.B.title) }),
  ).toHaveAttribute("aria-current", "page");
  await expect(main).toHaveAttribute("aria-busy", "false");
  await expect(page.getByRole("status")).toBeEmpty();
  await expect(page.getByRole("textbox", { name: "質問" })).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "スレッドを削除" }),
  ).toBeEnabled();
  expect(errors).toEqual([]);
});

test("modifier clickとmiddle clickは別tabへ渡す", async ({ page }) => {
  const aPath = `/research/${RESEARCH_THREADS.A.id}?limit=2`;
  const bPath = `/research/${RESEARCH_THREADS.B.id}?limit=2`;
  await page.goto(aPath);
  const bLink = page.locator(`a[href="${bPath}"]`);
  const modifier = process.platform === "darwin" ? "Meta" : "Control";

  const popupPromise = page.context().waitForEvent("page");
  await bLink.click({ modifiers: [modifier] });
  const popup = await popupPromise;
  await popup.close();
  await expect(page).toHaveURL(aPath);

  const middlePopupPromise = page.context().waitForEvent("page");
  await bLink.click({ button: "middle" });
  const middlePopup = await middlePopupPromise;
  await middlePopup.close();
  await expect(page).toHaveURL(aPath);
});

test("hard reload後のthread往復で旧pending stateを持ち越さない", async ({
  page,
}) => {
  const aPath = `/research/${RESEARCH_THREADS.A.id}?limit=2`;
  const bPath = `/research/${RESEARCH_THREADS.B.id}?limit=2`;
  const devtools = await page.context().newCDPSession(page);
  await devtools.send("Network.enable");
  await devtools.send("Network.setCacheDisabled", { cacheDisabled: true });

  await page.goto(aPath);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: RESEARCH_THREADS.A.title }),
  ).toBeVisible();

  await page.locator(`a[href="${bPath}"]`).click();
  await expect(page).toHaveURL(bPath);
  await expect(
    page.getByRole("heading", { name: RESEARCH_THREADS.B.title }),
  ).toBeVisible();
  await expect(page.getByRole("main")).toHaveAttribute("aria-busy", "false");

  await page
    .getByRole("link", { name: new RegExp(RESEARCH_THREADS.A.title) })
    .click();
  await expect(page).toHaveURL(aPath);
  await expect(
    page.getByRole("heading", { name: RESEARCH_THREADS.A.title }),
  ).toBeVisible();
  await expect(page.getByRole("main")).toHaveAttribute("aria-busy", "false");
  await expect(page.getByTestId("research-navigation-overlay")).toBeHidden();
  await expect(
    page.getByRole("link", { name: new RegExp(RESEARCH_THREADS.A.title) }),
  ).toHaveAttribute("aria-current", "page");
});

test("必須viewportでdocumentを固定しanswerとcomposerを独立配置する", async ({
  page,
}) => {
  test.slow();

  for (const viewport of REQUIRED_VIEWPORTS) {
    await test.step(`${viewport.width}x${viewport.height}`, async () => {
      await page.setViewportSize(viewport);
      await page.goto(alphaPath());
      await expect(page.getByText(RESEARCH_THREADS.A.answer)).toBeVisible();

      const scroller = answerPanel(page);
      const header = page
        .getByRole("heading", { name: RESEARCH_THREADS.A.title })
        .locator("xpath=ancestor::header");
      const sourcesTrigger = sourceTrigger(page);
      const dock = composer(page);
      await expectScrollable(scroller);

      const inlineSources = page.getByRole("complementary", {
        name: "ソース",
      });
      await expect(inlineSources).toHaveCount(0);
      await expect(sourcesTrigger).toHaveAttribute("aria-expanded", "false");
      await expect(sourcesTrigger).not.toHaveAttribute("aria-controls");
      const answerRailBox = await requiredBox(answerRail(page));
      const composerRailBox = await requiredBox(composerRail(page));
      const answerCenter = answerRailBox.x + answerRailBox.width / 2;
      const composerCenter = composerRailBox.x + composerRailBox.width / 2;
      const centerDelta = Math.abs(answerCenter - composerCenter);
      expect
        .soft(
          centerDelta,
          `${viewport.width}px rail centers: answer=${answerCenter}, composer=${composerCenter}, delta=${centerDelta}`,
        )
        .toBeLessThanOrEqual(1);

      const documentMetrics = await page.evaluate(() => ({
        scrollY: window.scrollY,
        height: document.documentElement.scrollHeight,
        clientHeight: document.documentElement.clientHeight,
        width: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(documentMetrics.scrollY).toBe(0);
      expect(documentMetrics.height).toBeLessThanOrEqual(
        documentMetrics.clientHeight,
      );
      expect(documentMetrics.width).toBeLessThanOrEqual(
        documentMetrics.clientWidth,
      );

      await page.evaluate(() => {
        window.scrollTo({ top: 500, left: 500 });
        document.documentElement.scrollTop = 500;
        document.body.scrollTop = 500;
      });
      await header.hover();
      await page.mouse.wheel(0, 700);
      expect(await page.evaluate(() => window.scrollY)).toBe(0);

      const headerBefore = await requiredBox(header);
      const triggerBefore = await requiredBox(sourcesTrigger);
      const composerBefore = await requiredBox(dock);
      await scrollByWheel(page, scroller);
      expectSameBox(await requiredBox(header), headerBefore);
      expectSameBox(await requiredBox(sourcesTrigger), triggerBefore);
      expectSameBox(await requiredBox(dock), composerBefore);

      await scroller.evaluate((element) => {
        element.scrollTop = element.scrollHeight;
      });
      const finalAnswer = await requiredBox(
        page.getByTestId("research-answer-slot").last(),
      );
      const composerAfterScroll = await requiredBox(dock);
      expect(finalAnswer.y + finalAnswer.height).toBeLessThanOrEqual(
        composerAfterScroll.y + 1,
      );
    });
  }
});

test("1023pxと1024pxで履歴drawerとinline sidebarの境界を保つ", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1023, height: 900 });
  await page.goto(alphaPath());
  const compactToggle = page.getByRole("button", { name: "履歴を開く" });
  await expect(page.locator("aside#research-history")).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "リサーチ履歴" })).toHaveCount(
    0,
  );
  await expect(compactToggle).toHaveAttribute("aria-expanded", "false");

  const compactAnswer = answerPanel(page);
  await compactAnswer.evaluate((element) => {
    element.scrollTop = 120;
  });
  const compactAnswerTop = await compactAnswer.evaluate(
    (element) => element.scrollTop,
  );
  const compactComposer = await requiredBox(composer(page));
  await compactToggle.click();
  const drawer = page.getByRole("dialog", { name: "リサーチ履歴" });
  const drawerClose = drawer.getByRole("button", { name: "履歴を閉じる" });
  await expect(drawer).toBeVisible();
  await expect(drawerClose).toBeFocused();
  const drawerHistory = drawer.getByRole("navigation", {
    name: "リサーチ履歴",
  });
  await scrollByWheel(page, drawerHistory);
  expect(await compactAnswer.evaluate((element) => element.scrollTop)).toBe(
    compactAnswerTop,
  );
  expectSameBox(await requiredBox(composer(page)), compactComposer);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(compactToggle).toBeFocused();

  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto(alphaPath());
  const sidebar = page.locator("aside#research-history");
  const desktopToggle = page.getByRole("button", { name: "履歴を閉じる" });
  await expect(sidebar).toBeVisible();
  await expect(page.getByRole("dialog", { name: "リサーチ履歴" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("complementary", { name: "ソース" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("dialog", { name: "ソース" })).toHaveCount(0);
  const desktopHistory = sidebar.getByRole("navigation", {
    name: "リサーチ履歴",
  });
  const desktopAnswer = answerPanel(page);
  const desktopAnswerTop = await desktopAnswer.evaluate(
    (element) => element.scrollTop,
  );
  const desktopComposerBefore = await requiredBox(composer(page));
  await scrollByWheel(page, desktopHistory);
  expect(await desktopAnswer.evaluate((element) => element.scrollTop)).toBe(
    desktopAnswerTop,
  );
  expectSameBox(await requiredBox(composer(page)), desktopComposerBefore);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);

  const answerBeforeClose = await requiredBox(desktopAnswer);
  await desktopToggle.click();
  await expect(sidebar).toHaveCount(0);
  const answerAfterClose = await requiredBox(desktopAnswer);
  const desktopComposerAfter = await requiredBox(composer(page));
  expect(answerAfterClose.width).toBeGreaterThan(answerBeforeClose.width);
  expect(
    Math.abs(desktopComposerAfter.y - desktopComposerBefore.y),
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs(
      desktopComposerAfter.y +
        desktopComposerAfter.height -
        (desktopComposerBefore.y + desktopComposerBefore.height),
    ),
  ).toBeLessThanOrEqual(1);
  await page.getByRole("button", { name: "履歴を開く" }).click();
  await expect(sidebar).toBeVisible();
});

test("1279pxと1280pxでsources sheetとinline panelを排他的に保つ", async ({
  page,
}) => {
  test.slow();

  for (const width of [1279, 1280]) {
    await test.step(`${width}px`, async () => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(alphaPath(2, true));
      await expect(page.getByText(RESEARCH_THREADS.A.answer)).toBeVisible();
      await expect(page.getByRole("tablist")).toHaveCount(0);
      await expect(page.getByRole("tab")).toHaveCount(0);
      await expect(page.getByRole("tabpanel")).toHaveCount(0);

      const textarea = page.getByRole("textbox", { name: "質問" });
      const trigger = sourceTrigger(page);
      const scroller = answerPanel(page);
      await textarea.fill(`sources-${width}`);
      await scroller.evaluate((element) => {
        element.scrollTop = 160;
      });
      const answerTop = await scroller.evaluate((element) => element.scrollTop);
      const composerBefore = await requiredBox(composer(page));
      await expectSourcesClosed(page);

      if (width === 1279) {
        await expect(
          page.getByRole("complementary", { name: "ソース" }),
        ).toHaveCount(0);
        await expect(page.getByRole("dialog", { name: "ソース" })).toHaveCount(
          0,
        );
        await trigger.click();
        const sheet = page.getByRole("dialog", { name: "ソース" });
        const close = sheet.getByRole("button", { name: "ソースを閉じる" });
        await expect(sheet).toBeVisible();
        await expect(close).toBeFocused();
        await expect(trigger).toHaveAttribute("aria-expanded", "true");
        await expect(trigger).toHaveAttribute(
          "aria-controls",
          "research-sources-sheet",
        );
        const sourceScroller = sheet.locator(".overflow-y-auto");
        await expect(
          sourceScroller.locator(`a[href="${RESEARCH_SOURCE_HREF}"]`),
        ).toBeVisible();
        await scrollByWheel(page, sourceScroller);
        expect(await scroller.evaluate((element) => element.scrollTop)).toBe(
          answerTop,
        );
        expectSameBox(await requiredBox(composer(page)), composerBefore);
        expect(await page.evaluate(() => window.scrollY)).toBe(0);
        await page.keyboard.press("Escape");
        await expect(sheet).toHaveCount(0);
        await expect(trigger).toBeFocused();
        await expect(trigger).toHaveAttribute("aria-expanded", "false");
        await expect(trigger).not.toHaveAttribute("aria-controls");

        await expectResearchHrefsWithoutView(page);
      } else {
        const inline = page.getByRole("complementary", { name: "ソース" });
        await expect(page.getByRole("dialog", { name: "ソース" })).toHaveCount(
          0,
        );
        await trigger.click();
        await expect(inline).toBeVisible();
        await expect(trigger).toHaveAttribute("aria-expanded", "true");
        await expect(trigger).toHaveAttribute(
          "aria-controls",
          "research-sources-inline",
        );
        const composerAfterOpen = await requiredBox(composer(page));
        expect(
          Math.abs(composerBefore.width - composerAfterOpen.width - 320),
        ).toBeLessThanOrEqual(1);
        const sourceScroller = inline.locator(".overflow-y-auto");
        await expect(
          sourceScroller.locator(`a[href="${RESEARCH_SOURCE_HREF}"]`),
        ).toBeVisible();
        await scrollByWheel(page, sourceScroller);
        expect(await scroller.evaluate((element) => element.scrollTop)).toBe(
          answerTop,
        );
        expectSameBox(await requiredBox(composer(page)), composerAfterOpen);
        expect(await page.evaluate(() => window.scrollY)).toBe(0);
        await trigger.click();
        await expect(inline).toHaveCount(0);
        await expect(trigger).toBeFocused();
        await expect(trigger).toHaveAttribute("aria-expanded", "false");
        await expect(trigger).not.toHaveAttribute("aria-controls");
        await trigger.click();
        await expect(inline).toBeVisible();
        await expectResearchHrefsWithoutView(page);
      }

      await expect(textarea).toHaveValue(`sources-${width}`);
      expect(await scroller.evaluate((element) => element.scrollTop)).toBe(
        answerTop,
      );
      const composerAfter = await requiredBox(composer(page));
      if (width === 1279) {
        expectSameBox(composerAfter, composerBefore);
      } else {
        expect(
          Math.abs(composerBefore.width - composerAfter.width - 320),
        ).toBeLessThanOrEqual(1);
        expect(
          Math.abs(composerBefore.y - composerAfter.y),
        ).toBeLessThanOrEqual(1);
        expect(
          Math.abs(composerBefore.height - composerAfter.height),
        ).toBeLessThanOrEqual(1);
      }
      await expect(trigger).toContainText(String(RESEARCH_SOURCE_COUNT));
      await expect(page.getByRole("dialog", { name: "ソース" })).toHaveCount(0);
    });
  }
});
