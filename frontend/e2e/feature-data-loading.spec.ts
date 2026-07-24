import { type Browser, expect, type Page, test } from "@playwright/test";
import { startFeatureDataRunner } from "./fixtures/feature-data-runner";
import { RESEARCH_THREADS } from "./fixtures/research";

const USER_STORAGE_STATE = "e2e/.auth/user.json";

type GateScenario = {
  scenario: string;
  heldPathname: string;
  pathname: string;
  assertFallback: (page: Page) => Promise<void>;
};

async function discoverHref(
  page: Page,
  sourcePath: string,
  selector: string,
): Promise<string> {
  await page.goto(sourcePath);
  const link = page.locator(selector).first();
  await expect(link).toBeVisible();
  const href = await link.getAttribute("href");
  if (href === null) {
    throw new Error(`Expected an href for ${selector}`);
  }
  return href;
}

function decodeRouteSegment(href: string, prefix: string): string {
  const pathname = new URL(href, "http://vector.local").pathname;
  const matched = pathname.match(new RegExp(`^${prefix}/([^/]+)$`));
  if (matched?.[1] === undefined) {
    throw new Error(`Expected ${href} to match ${prefix}/<segment>`);
  }
  try {
    return decodeURIComponent(matched[1]);
  } catch {
    throw new Error(`Expected ${href} to contain a valid URL-encoded segment`);
  }
}

function articleDetailEndpoint(href: string): string {
  const articleId = decodeRouteSegment(href, "/news");
  if (
    !/^[1-9]\d*$/.test(articleId) ||
    !Number.isSafeInteger(Number(articleId))
  ) {
    throw new Error(`Expected a positive safe article ID in ${href}`);
  }
  return `/api/v1/articles/${articleId}`;
}

function briefingDetailEndpoint(href: string): string {
  const categorySlug = decodeRouteSegment(href, "/briefing");
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(categorySlug)) {
    throw new Error(`Expected a canonical briefing category slug in ${href}`);
  }
  return `/api/v1/briefing/${encodeURIComponent(categorySlug)}`;
}

async function runFeatureDataScenario(
  browser: Browser,
  scenario: GateScenario,
): Promise<void> {
  const runner = await startFeatureDataRunner({
    scenario: scenario.scenario,
    readyPathname: scenario.pathname,
    storageStatePath: USER_STORAGE_STATE,
    heldPathname: scenario.heldPathname,
  });
  const context = await browser.newContext({
    storageState: USER_STORAGE_STATE,
  });
  const page = await context.newPage();
  let navigation: Promise<unknown> | undefined;

  try {
    navigation = page.goto(`${runner.baseURL}${scenario.pathname}`, {
      waitUntil: "domcontentloaded",
    });
    await runner.gate.waitForHit();
    expect(runner.gate.hitCount()).toBe(1);
    await scenario.assertFallback(page);
  } finally {
    runner.gate.release();
    await navigation?.catch(() => undefined);
    await context.close();
    await runner.dispose();
  }
}

test.describe("feature-data loading feedback", () => {
  test.describe.configure({ timeout: 120_000 });

  test("fresh dashboard data exposes the article-grid fallback", async ({
    browser,
  }) => {
    await runFeatureDataScenario(browser, {
      scenario: "dashboard-initial",
      heldPathname: "/api/v1/articles",
      pathname: "/",
      assertFallback: async (page) => {
        await expect(page.getByText("Vector ニュースへ")).toBeVisible();
        await expect(
          page.getByRole("status", { name: /記事を.+中…/ }),
        ).toBeVisible();
        await expect(page.getByTestId("page-navigation-overlay")).toHaveCount(
          0,
        );
      },
    });
  });

  test("fresh news detail data exposes its own shell instead of the dashboard grid", async ({
    page,
    browser,
  }) => {
    const articlePath = await discoverHref(page, "/", 'a[href^="/news/"]');

    await runFeatureDataScenario(browser, {
      scenario: "news-detail",
      heldPathname: articleDetailEndpoint(articlePath),
      pathname: articlePath,
      assertFallback: async (freshPage) => {
        await expect(
          freshPage.getByRole("status", { name: "記事を読み込み中…" }),
        ).toBeVisible();
        await expect(freshPage.getByText("WATCHLIST")).toHaveCount(0);
      },
    });
  });

  test("fresh briefing detail data exposes the briefing fallback", async ({
    page,
    browser,
  }) => {
    const briefingPath = await discoverHref(
      page,
      "/briefing",
      'a[href^="/briefing/"]',
    );

    await runFeatureDataScenario(browser, {
      scenario: "briefing-detail",
      heldPathname: briefingDetailEndpoint(briefingPath),
      pathname: briefingPath,
      assertFallback: async (freshPage) => {
        await expect(
          freshPage.getByRole("status", { name: "Briefingを読み込み中…" }),
        ).toBeVisible();
        await expect(freshPage.getByText("WATCHLIST")).toHaveCount(0);
      },
    });
  });

  test("fresh research thread data shows no thread-private value before its workspace resolves", async ({
    browser,
  }) => {
    await runFeatureDataScenario(browser, {
      scenario: "research-thread",
      heldPathname: `/api/v1/research/threads/${RESEARCH_THREADS.A.id}`,
      pathname: `/research/${RESEARCH_THREADS.A.id}`,
      assertFallback: async (page) => {
        await expect(
          page.getByRole("status", { name: "Researchを読み込み中…" }),
        ).toBeVisible();
        await expect(page.getByText(RESEARCH_THREADS.A.title)).toHaveCount(0);
      },
    });
  });

  test("dashboard category URL keeps loading feedback local to the article region", async ({
    page,
    browser,
  }) => {
    const categoryPath = await discoverHref(page, "/", 'a[href*="category="]');

    await runFeatureDataScenario(browser, {
      scenario: "dashboard-category",
      heldPathname: "/api/v1/articles",
      pathname: categoryPath,
      assertFallback: async (freshPage) => {
        await expect(
          freshPage.getByRole("status", { name: "記事を更新中…" }),
        ).toBeVisible();
        await expect(
          freshPage.getByTestId("page-navigation-overlay"),
        ).toHaveCount(0);
      },
    });
  });
});
