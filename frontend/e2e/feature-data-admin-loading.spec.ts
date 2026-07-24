import { type Browser, expect, type Page, test } from "@playwright/test";
import { startFeatureDataRunner } from "./fixtures/feature-data-runner";

const ADMIN_STORAGE_STATE = "e2e/.auth/admin.json";

type AdminScenario = {
  scenario: string;
  heldPathname: string;
  pathname: string;
  status: string;
  heading: string;
};

async function runAdminFeatureDataScenario(
  browser: Browser,
  scenario: AdminScenario,
): Promise<void> {
  const runner = await startFeatureDataRunner({
    scenario: scenario.scenario,
    readyPathname: scenario.pathname,
    storageStatePath: ADMIN_STORAGE_STATE,
    heldPathname: scenario.heldPathname,
  });
  const context = await browser.newContext({
    storageState: ADMIN_STORAGE_STATE,
  });
  const page: Page = await context.newPage();
  let navigation: Promise<unknown> | undefined;

  try {
    navigation = page.goto(`${runner.baseURL}${scenario.pathname}`, {
      waitUntil: "domcontentloaded",
    });
    await runner.gate.waitForHit();
    expect(runner.gate.hitCount()).toBe(1);
    await expect(
      page.getByRole("heading", { name: scenario.heading }),
    ).toBeVisible();
    await expect(
      page.getByRole("status", { name: scenario.status }),
    ).toBeVisible();
    await expect(page.getByTestId("page-navigation-overlay")).toHaveCount(0);
  } finally {
    runner.gate.release();
    await navigation?.catch(() => undefined);
    await context.close();
    await runner.dispose();
  }
}

test.describe("admin feature-data loading feedback", () => {
  test.describe.configure({ timeout: 120_000 });

  test("settings keeps its page frame while sources are pending", async ({
    browser,
  }) => {
    await runAdminFeatureDataScenario(browser, {
      scenario: "admin-settings",
      heldPathname: "/api/v1/admin/sources",
      pathname: "/settings",
      status: "設定を読み込み中…",
      heading: "Settings",
    });
  });

  test("pipeline status keeps its in-page fallback while health data is pending", async ({
    browser,
  }) => {
    await runAdminFeatureDataScenario(browser, {
      scenario: "admin-pipeline-status",
      heldPathname: "/api/v1/admin/pipeline/health",
      pathname: "/admin/pipeline-status",
      status: "パイプライン状況を読み込み中…",
      heading: "Pipeline Status",
    });
  });

  test("source health keeps its in-page fallback while health data is pending", async ({
    browser,
  }) => {
    await runAdminFeatureDataScenario(browser, {
      scenario: "admin-source-health",
      heldPathname: "/api/v1/admin/sources/health",
      pathname: "/admin/source-health",
      status: "ソース健全性を読み込み中…",
      heading: "Source Health",
    });
  });
});
