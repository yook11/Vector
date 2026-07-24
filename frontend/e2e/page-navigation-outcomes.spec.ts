import { readFile } from "node:fs/promises";
import path from "node:path";
import {
  type BrowserContext,
  expect,
  type Page,
  type Route,
  test,
} from "@playwright/test";
import { startFeatureDataRunner } from "./fixtures/feature-data-runner";
import {
  RESEARCH_EXPANDED_HISTORY_LIMIT,
  RESEARCH_THREADS,
} from "./fixtures/research";

const FRONTEND_DIRECTORY = path.resolve(__dirname, "..");
const ADMIN_STORAGE_STATE = path.join(
  FRONTEND_DIRECTORY,
  "e2e/.auth/admin.json",
);
const USER_STORAGE_STATE = path.join(FRONTEND_DIRECTORY, "e2e/.auth/user.json");

type ResponseGate = {
  waitForResponse: () => Promise<number>;
  release: () => void;
  settle: () => Promise<void>;
};

type GlobalNavigationObservation = {
  ariaBusy: boolean;
  overlay: boolean;
  status: boolean;
};

function isNavigationRsc(route: Route, pathname: string): boolean {
  const request = route.request();
  const headers = request.headers();
  return (
    request.method() === "GET" &&
    new URL(request.url()).pathname === pathname &&
    headers.rsc !== undefined &&
    headers["next-router-prefetch"] !== "1" &&
    headers.purpose !== "prefetch"
  );
}

async function installRscResponseGates(
  page: Page,
  pathnames: readonly string[],
  options: { abortPrefetch?: boolean } = {},
): Promise<{
  gates: ReadonlyMap<string, ResponseGate>;
  remove: () => Promise<void>;
}> {
  const states = new Map<
    string,
    {
      release: Promise<void>;
      releaseNow: () => void;
      response: Promise<number>;
      reportResponse: (status: number) => void;
      rejectResponse: (error: unknown) => void;
      settled: Promise<void>;
      reportSettled: () => void;
      handled: boolean;
    }
  >();
  for (const pathname of pathnames) {
    const release = Promise.withResolvers<void>();
    const response = Promise.withResolvers<number>();
    const settled = Promise.withResolvers<void>();
    states.set(pathname, {
      release: release.promise,
      releaseNow: release.resolve,
      response: response.promise,
      reportResponse: response.resolve,
      rejectResponse: response.reject,
      settled: settled.promise,
      reportSettled: settled.resolve,
      handled: false,
    });
  }

  const handler = async (route: Route) => {
    const pathname = new URL(route.request().url()).pathname;
    const state = states.get(pathname);
    if (state === undefined) {
      await route.continue();
      return;
    }

    const requestHeaders = route.request().headers();
    const isPrefetch =
      requestHeaders.rsc !== undefined &&
      (requestHeaders["next-router-prefetch"] === "1" ||
        requestHeaders.purpose === "prefetch");
    if (options.abortPrefetch === true && isPrefetch) {
      await route.abort("blockedbyclient");
      return;
    }
    if (state.handled || !isNavigationRsc(route, pathname)) {
      await route.continue();
      return;
    }

    state.handled = true;
    try {
      const response = await route.fetch({
        maxRedirects: 0,
        timeout: 30_000,
      });
      state.reportResponse(response.status());
      await state.release;
      await route.fulfill({ response });
    } catch (error) {
      state.rejectResponse(error);
      throw error;
    } finally {
      state.reportSettled();
    }
  };
  await page.route("**/*", handler);

  return {
    gates: new Map(
      Array.from(states, ([pathname, state]) => [
        pathname,
        {
          waitForResponse: () => state.response,
          release: state.releaseNow,
          settle: () => state.settled,
        },
      ]),
    ),
    remove: async () => {
      const handled = Array.from(states.values()).filter(
        (state) => state.handled,
      );
      for (const state of handled) state.releaseNow();
      await Promise.allSettled(handled.map((state) => state.settled));
      await page.unroute("**/*", handler);
    },
  };
}

function requiredGate(
  gates: ReadonlyMap<string, ResponseGate>,
  pathname: string,
): ResponseGate {
  const gate = gates.get(pathname);
  if (gate === undefined) throw new Error(`Missing RSC gate for ${pathname}`);
  return gate;
}

async function expectNoStalePageNavigation(page: Page): Promise<void> {
  await expect(page.getByRole("status", { name: /を読み込み中…/ })).toHaveCount(
    0,
  );
  await expect(page.getByTestId("page-navigation-overlay")).toHaveCount(0);
  await expect(page.locator("[aria-busy='true']")).toHaveCount(0);
}

async function replaceContextCookiesWithUser(
  context: BrowserContext,
): Promise<void> {
  const state = JSON.parse(
    await readFile(USER_STORAGE_STATE, "utf8"),
  ) as Awaited<ReturnType<BrowserContext["storageState"]>>;
  await context.clearCookies();
  await context.addCookies(state.cookies);
}

function articleDetailEndpoint(href: string): string {
  const pathname = new URL(href, "http://vector.local").pathname;
  const articleId = pathname.match(/^\/news\/([1-9]\d*)$/)?.[1];
  if (articleId === undefined || !Number.isSafeInteger(Number(articleId))) {
    throw new Error(`Expected a positive safe article ID in ${href}`);
  }
  return `/api/v1/articles/${articleId}`;
}

async function installRejectedOverlapObserver(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const observedWindow = window as Window & {
      __researchRejectedOverlap?: boolean;
    };
    observedWindow.__researchRejectedOverlap = false;

    const sample = () => {
      const rejected = document.querySelector("[data-research-route-rejected]");
      const initial = document.querySelector<HTMLElement>(
        "[data-research-route-initial]",
      );
      if (rejected === null || initial === null) return;
      const initialStatus = initial.querySelector<HTMLElement>(
        '[role="status"][aria-label="Researchを読み込み中…"]',
      );
      if (initialStatus === null) return;
      const style = getComputedStyle(initialStatus);
      const bounds = initialStatus.getBoundingClientRect();
      const visible =
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        bounds.width > 0 &&
        bounds.height > 0;
      if (visible) observedWindow.__researchRejectedOverlap = true;
    };

    new MutationObserver(sample).observe(document, {
      childList: true,
      subtree: true,
    });
    const sampleFrame = () => {
      sample();
      requestAnimationFrame(sampleFrame);
    };
    requestAnimationFrame(sampleFrame);
  });
}

async function installGlobalNavigationObserver(
  page: Page,
  label: string,
): Promise<GlobalNavigationObservation> {
  const observation: GlobalNavigationObservation = {
    ariaBusy: false,
    overlay: false,
    status: false,
  };
  await page.exposeBinding(
    "__reportGlobalNavigationFeedback",
    ({ frame }, key: keyof GlobalNavigationObservation) => {
      if (frame === page.mainFrame()) observation[key] = true;
    },
  );
  await page.addInitScript((expectedLabel) => {
    const observedWindow = window as Window & {
      __reportGlobalNavigationFeedback?: (
        key: keyof GlobalNavigationObservation,
      ) => Promise<void>;
    };
    const reported: GlobalNavigationObservation = {
      ariaBusy: false,
      overlay: false,
      status: false,
    };

    const visible = (element: HTMLElement | null) => {
      if (element === null) return false;
      const style = getComputedStyle(element);
      const bounds = element.getBoundingClientRect();
      return (
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        bounds.width > 0 &&
        bounds.height > 0
      );
    };
    const sample = () => {
      const statusVisible = Array.from(
        document.querySelectorAll<HTMLElement>('[role="status"]'),
      ).some(
        (candidate) =>
          candidate.getAttribute("aria-label") === expectedLabel &&
          visible(candidate),
      );
      const overlayVisible = Array.from(
        document.querySelectorAll<HTMLElement>(
          '[data-testid="page-navigation-overlay"]',
        ),
      ).some(visible);
      const ariaBusyVisible = Array.from(
        document.querySelectorAll<HTMLElement>('[aria-busy="true"]'),
      ).some(visible);
      const report = observedWindow.__reportGlobalNavigationFeedback;
      for (const [key, isVisible] of Object.entries({
        ariaBusy: ariaBusyVisible,
        overlay: overlayVisible,
        status: statusVisible,
      }) as [keyof GlobalNavigationObservation, boolean][]) {
        if (isVisible && !reported[key]) {
          reported[key] = true;
          void report?.(key);
        }
      }
    };

    new MutationObserver(sample).observe(document, {
      attributes: true,
      childList: true,
      subtree: true,
    });
    const sampleFrame = () => {
      sample();
      requestAnimationFrame(sampleFrame);
    };
    requestAnimationFrame(sampleFrame);
  }, label);
  return observation;
}

async function expectGlobalNavigationObserved(
  observation: GlobalNavigationObservation,
): Promise<void> {
  await expect
    .poll(() => observation, { timeout: 5_000 })
    .toEqual({ ariaBusy: true, overlay: true, status: true });
}

async function startHydratedLinkNavigation(
  page: Page,
  href: string,
): Promise<void> {
  await page.waitForFunction(
    (expectedHref) => {
      const nextRouter = (
        window as Window & {
          next?: { router?: { push?: unknown } };
        }
      ).next?.router;
      if (typeof nextRouter?.push !== "function") return false;
      const link = Array.from(document.querySelectorAll("a")).find(
        (candidate) => candidate.getAttribute("href") === expectedHref,
      );
      return link !== undefined;
    },
    href,
    { timeout: 30_000 },
  );
  const hydratedThemeToggle = page.getByRole("button", {
    name: /^(ダーク|ライト)テーマに切り替え$/,
  });
  await expect(hydratedThemeToggle).toBeVisible({ timeout: 30_000 });
  await expect(hydratedThemeToggle).toBeEnabled();
  await page.locator(`a[href="${href}"]`).click({ noWaitAfter: true });
}

test("A→BでA responseが先にcommitしてもB statusだけを保持してBへ収束する", async ({
  page,
}) => {
  test.slow();
  await page.goto("/");
  const controls = await installRscResponseGates(page, [
    "/research",
    "/briefing",
  ]);
  const researchGate = requiredGate(controls.gates, "/research");
  const briefingGate = requiredGate(controls.gates, "/briefing");

  try {
    await page.getByRole("link", { name: "Research" }).first().click();
    await researchGate.waitForResponse();
    await page.getByRole("link", { name: "Briefing" }).first().click({
      force: true,
    });
    await briefingGate.waitForResponse();
    await expect(
      page.getByRole("status", { name: "Briefingを読み込み中…" }),
    ).toBeVisible();

    researchGate.release();
    await researchGate.settle();
    await expect(
      page.getByRole("status", { name: "Briefingを読み込み中…" }),
    ).toBeVisible();
    await expect(page.getByTestId("page-navigation-overlay")).toBeVisible();

    briefingGate.release();
    await briefingGate.settle();
  } finally {
    researchGate.release();
    briefingGate.release();
    await controls.remove();
  }

  await expect(page).toHaveURL(/\/briefing(?:\?|$)/);
  await expect(
    page.getByRole("heading", { name: "今週のブリーフィング" }),
  ).toBeVisible();
  await expectNoStalePageNavigation(page);
});

for (const scenario of [
  { label: "same URL", origin: "/" },
  { label: "other URL", origin: "/briefing" },
] as const) {
  test(`${scenario.label}へのauth redirect後にpendingを残さない`, async ({
    browser,
  }) => {
    test.slow();
    const context = await browser.newContext({
      storageState: ADMIN_STORAGE_STATE,
    });
    const page = await context.newPage();
    const controls = await installRscResponseGates(page, ["/settings"]);
    const gate = requiredGate(controls.gates, "/settings");

    try {
      await page.goto(scenario.origin);
      const settings = page.getByRole("link", { name: "Settings" }).first();
      await expect(settings).toBeVisible();
      await replaceContextCookiesWithUser(context);

      await settings.click();
      expect(await gate.waitForResponse()).toBeLessThan(400);
      await expect(
        page.getByRole("status", { name: "Settingsを読み込み中…" }),
      ).toBeVisible();
      gate.release();
      await gate.settle();
      await expect(page).toHaveURL((url) => url.pathname === "/");
      await expect(
        page.getByRole("link", { name: "Vector ニュースへ" }),
      ).toBeVisible();
      await expectNoStalePageNavigation(page);
    } finally {
      gate.release();
      await controls.remove();
      await context.close();
    }
  });
}

for (const scenario of [
  {
    label: "not-found",
    response: { status: 404, body: '{"detail":"Not found"}' },
    assertOutcome: async (page: Page) => {
      await expect(page.getByText("Article not found.")).toBeVisible({
        timeout: 30_000,
      });
    },
  },
  {
    label: "error",
    response: { status: 500, body: '{"detail":"forced E2E failure"}' },
    assertOutcome: async (page: Page) => {
      await expect(
        page.getByRole("heading", { name: "記事の取得に失敗しました" }),
      ).toBeVisible({ timeout: 30_000 });
    },
  },
] as const) {
  test(`PendingAwareLinkのglobal ${scenario.label} outcomeでpendingを残さない`, async ({
    browser,
    page: seededPage,
  }) => {
    test.setTimeout(180_000);
    await seededPage.goto("/", {
      timeout: 45_000,
      waitUntil: "commit",
    });
    const articleLinks = seededPage.locator('a[href^="/news/"]');
    const articleLink = articleLinks.first();
    const compileArticleLink = articleLinks.nth(1);
    await expect(articleLink).toBeVisible({ timeout: 30_000 });
    await expect(compileArticleLink).toBeVisible({ timeout: 30_000 });
    const [articlePath, compileArticlePath] = await Promise.all([
      articleLink.getAttribute("href"),
      compileArticleLink.getAttribute("href"),
    ]);
    if (articlePath === null || compileArticlePath === null) {
      throw new Error("Expected dashboard article links to have href values");
    }

    const runner = await startFeatureDataRunner({
      scenario: `global-navigation-${scenario.label}`,
      readyPathname: compileArticlePath,
      storageStatePath: USER_STORAGE_STATE,
      heldPathname: articleDetailEndpoint(articlePath),
      holdMs: 0,
      response: scenario.response,
    });
    const context = await browser.newContext({
      storageState: USER_STORAGE_STATE,
    });
    const page = await context.newPage();
    const observation = await installGlobalNavigationObserver(
      page,
      "記事を読み込み中…",
    );
    const controls = await installRscResponseGates(page, [articlePath], {
      abortPrefetch: true,
    });
    const rscGate = requiredGate(controls.gates, articlePath);

    try {
      await page.goto(runner.baseURL, {
        timeout: 45_000,
        waitUntil: "commit",
      });
      const pendingAwareArticleLink = page.locator(`a[href="${articlePath}"]`);
      await expect(pendingAwareArticleLink).toBeVisible({
        timeout: 30_000,
      });
      await startHydratedLinkNavigation(page, articlePath);
      await expectGlobalNavigationObserved(observation);
      await runner.gate.waitForHit();

      runner.gate.release();
      expect(await rscGate.waitForResponse()).toBeLessThan(400);
      rscGate.release();
      await rscGate.settle();
      try {
        await scenario.assertOutcome(page);
      } catch (error) {
        const assertion =
          error instanceof Error ? error.message : String(error);
        throw new Error(
          [
            assertion,
            "[fresh frontend child diagnostics]",
            runner.diagnostics() || "(no actionable child output)",
          ].join("\n"),
        );
      }
      await expectNoStalePageNavigation(page);
    } finally {
      runner.gate.release();
      rscGate.release();
      await controls.remove();
      await context.close();
      await runner.dispose();
    }
  });
}

for (const scenario of [
  {
    label: "not-found",
    response: { status: 404, body: '{"detail":"Not found"}' },
    assertOutcome: async (page: Page) => {
      await expect(page.getByText("Research thread not found.")).toBeVisible({
        timeout: 30_000,
      });
    },
  },
  {
    label: "error",
    response: { status: 500, body: '{"detail":"forced E2E failure"}' },
    assertOutcome: async (page: Page) => {
      await expect(
        page.getByRole("heading", {
          name: "Researchの読み込みに失敗しました",
        }),
      ).toBeVisible({ timeout: 30_000 });
    },
  },
] as const) {
  test(`Research navigationの${scenario.label} outcomeでretained contentとpendingを残さない`, async ({
    browser,
  }) => {
    test.setTimeout(180_000);
    const researchPathSuffix = `?limit=${RESEARCH_EXPANDED_HISTORY_LIMIT}`;
    const runner = await startFeatureDataRunner({
      scenario: `research-navigation-${scenario.label}`,
      readyPathname: `/research/${RESEARCH_THREADS.A.id}${researchPathSuffix}`,
      storageStatePath: USER_STORAGE_STATE,
      heldPathname: `/api/v1/research/threads/${RESEARCH_THREADS.B.id}`,
      holdMs: 0,
      response: scenario.response,
    });
    const context = await browser.newContext({
      storageState: USER_STORAGE_STATE,
    });
    const page = await context.newPage();

    try {
      await page.goto(
        `${runner.baseURL}/research/${RESEARCH_THREADS.A.id}${researchPathSuffix}`,
        { timeout: 45_000, waitUntil: "domcontentloaded" },
      );
      await page.waitForLoadState("load");
      await expect(page.getByText(RESEARCH_THREADS.A.answer)).toBeVisible({
        timeout: 30_000,
      });
      const targetPath = `/research/${RESEARCH_THREADS.B.id}${researchPathSuffix}`;
      await expect(page.locator(`a[href="${targetPath}"]`)).toBeVisible();
      await startHydratedLinkNavigation(page, targetPath);
      await runner.gate.waitForHit();
      const researchNavigationOverlay = page.getByTestId(
        "research-navigation-overlay",
      );
      await expect(researchNavigationOverlay).toBeVisible();
      await expect(researchNavigationOverlay).toContainText(
        `「${RESEARCH_THREADS.B.title}」を読み込み中…`,
      );
      await expect(
        page.getByRole("status", { name: "Researchを読み込み中…" }),
      ).toHaveCount(0);
      await expect(page.getByTestId("page-navigation-overlay")).toHaveCount(0);
      await expect(page.getByText(RESEARCH_THREADS.A.answer)).toBeVisible();

      runner.gate.release();
      await scenario.assertOutcome(page);
      await expect(page.getByText(RESEARCH_THREADS.A.answer)).toHaveCount(0);
      await expect(page.getByText(RESEARCH_THREADS.A.title)).toHaveCount(0);
      await expectNoStalePageNavigation(page);
    } finally {
      runner.gate.release();
      await context.close();
      await runner.dispose();
    }
  });
}

for (const scenario of [
  {
    label: "not-found",
    response: { status: 404, body: '{"detail":"Not found"}' },
    assertOutcome: async (page: Page) => {
      await expect(page.getByText("Research thread not found.")).toBeVisible({
        timeout: 30_000,
      });
    },
  },
  {
    label: "error",
    response: { status: 500, body: '{"detail":"forced E2E failure"}' },
    assertOutcome: async (page: Page) => {
      await expect(
        page.getByRole("heading", {
          name: "Researchの読み込みに失敗しました",
        }),
      ).toBeVisible({ timeout: 30_000 });
    },
  },
] as const) {
  test(`Research direct hard-loadの${scenario.label} outcomeでinitial skeletonを同時表示しない`, async ({
    browser,
  }) => {
    test.setTimeout(180_000);
    const runner = await startFeatureDataRunner({
      scenario: `research-direct-${scenario.label}`,
      readyPathname: `/research/${RESEARCH_THREADS.A.id}?limit=2`,
      storageStatePath: USER_STORAGE_STATE,
      heldPathname: `/api/v1/research/threads/${RESEARCH_THREADS.B.id}`,
      holdMs: 0,
      response: scenario.response,
    });
    const context = await browser.newContext({
      storageState: USER_STORAGE_STATE,
    });
    const page = await context.newPage();
    await installRejectedOverlapObserver(page);
    const navigation = page.goto(
      `${runner.baseURL}/research/${RESEARCH_THREADS.B.id}?limit=2`,
      { timeout: 45_000, waitUntil: "domcontentloaded" },
    );

    try {
      await runner.gate.waitForHit();
      await expect(
        page.getByRole("status", { name: "Researchを読み込み中…" }),
      ).toBeVisible();
      await expect(page.getByText("Research thread not found.")).toHaveCount(0);
      await expect(
        page.getByRole("heading", {
          name: "Researchの読み込みに失敗しました",
        }),
      ).toHaveCount(0);

      runner.gate.release();
      await navigation;
      await scenario.assertOutcome(page);
      await expect(page.locator("[data-research-route-initial]")).toHaveCount(
        0,
      );
      expect(
        await page.evaluate(
          () =>
            (
              window as Window & {
                __researchRejectedOverlap?: boolean;
              }
            ).__researchRejectedOverlap,
        ),
      ).toBe(false);
      await expectNoStalePageNavigation(page);
    } finally {
      runner.gate.release();
      await navigation.catch(() => undefined);
      await context.close();
      await runner.dispose();
    }
  });
}
