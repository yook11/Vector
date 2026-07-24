import { expect, type Page, type Route, test } from "@playwright/test";

type RouteResponseGate = {
  blocked: () => boolean;
  release: () => void;
  settle: () => Promise<void>;
  remove: () => Promise<void>;
};

async function holdNavigationRsc(
  page: Page,
  targetPathname: string,
  responseReadyBeforeRelease = false,
): Promise<RouteResponseGate> {
  let isBlocked = false;
  let releaseGate: (() => void) | undefined;
  let completeGate: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => {
    releaseGate = resolve;
  });
  const settled = new Promise<void>((resolve) => {
    completeGate = resolve;
  });

  async function handler(route: Route) {
    const request = route.request();
    const headers = request.headers();
    const pathname = new URL(request.url()).pathname;
    const isNavigationRsc =
      pathname === targetPathname &&
      headers.rsc !== undefined &&
      headers["next-router-prefetch"] !== "1" &&
      !isBlocked;

    if (!isNavigationRsc) {
      await route.continue();
      return;
    }

    isBlocked = true;
    try {
      const response = responseReadyBeforeRelease
        ? await route.fetch({ maxRedirects: 0, timeout: 30_000 })
        : null;
      await gate;
      if (response === null) {
        await route.continue();
      } else {
        await route.fulfill({ response });
      }
    } finally {
      completeGate?.();
    }
  }

  await page.route("**/*", handler);

  return {
    blocked: () => isBlocked,
    release: () => releaseGate?.(),
    settle: () => settled,
    remove: () => page.unroute("**/*", handler),
  };
}

function targetPathname(href: string | null, prefix: string): string {
  expect(href).not.toBeNull();
  if (href === null) {
    throw new Error("navigation link must have an href");
  }
  const pathname = new URL(href, "http://vector.local").pathname;
  expect(pathname.startsWith(prefix)).toBe(true);
  return pathname;
}

test.describe("page navigation loading feedback", () => {
  test("desktop: DashboardからResearchへのroute response待機で旧画面、status、overlayを保つ", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(
      page.getByRole("link", { name: "Vector ニュースへ" }),
    ).toBeVisible();

    const gate = await holdNavigationRsc(page, "/research");
    try {
      await page.getByRole("link", { name: "Research" }).first().click();
      await page.waitForTimeout(250);

      expect(gate.blocked()).toBe(true);
      await expect(
        page.getByRole("link", { name: "Vector ニュースへ" }),
      ).toBeVisible();
      await expect(
        page.getByRole("status", { name: "Researchを読み込み中…" }),
      ).toBeVisible({ timeout: 100 });
      await expect(page.getByTestId("page-navigation-overlay")).toBeVisible({
        timeout: 100,
      });
    } finally {
      if (gate.blocked()) {
        gate.release();
        await gate.settle();
      }
      await gate.remove();
    }

    await expect(page).toHaveURL(/\/research(?:\?|$)/);
    await expect(
      page.getByRole("status", { name: "Researchを読み込み中…" }),
    ).toHaveCount(0);
  });

  test("mobile: DashboardからBriefingへのroute response待機中もSheet内statusと別linkを保つ", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await page.getByRole("button", { name: "メニュー" }).click();
    const sheet = page.getByRole("dialog", { name: "Vector" });
    await expect(sheet).toBeVisible();

    const gate = await holdNavigationRsc(page, "/briefing");
    try {
      await sheet.getByRole("link", { name: "Briefing" }).click();
      await page.waitForTimeout(250);

      expect(gate.blocked()).toBe(true);
      await expect(sheet).toBeVisible();
      await expect(
        sheet.getByRole("status", { name: "Briefingを読み込み中…" }),
      ).toBeVisible({ timeout: 100 });
      await expect(sheet.getByRole("link", { name: "トレンド" })).toBeEnabled({
        timeout: 100,
      });
    } finally {
      if (gate.blocked()) {
        gate.release();
        await gate.settle();
      }
      await gate.remove();
    }

    await expect(page).toHaveURL(/\/briefing(?:\?|$)/);
    await expect(sheet).toHaveCount(0);
  });

  test("mobile: pending中にmanual closeしてsettle後にreopenしてもSheetを維持する", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    const trigger = page.getByRole("button", { name: "メニュー" });
    await trigger.click();
    const sheet = page.getByRole("dialog", { name: "Vector" });
    await expect(sheet).toBeVisible();

    const gate = await holdNavigationRsc(page, "/briefing", true);
    try {
      await sheet.getByRole("link", { name: "Briefing" }).click();
      await expect.poll(gate.blocked).toBe(true);
      await expect(
        sheet.getByRole("status", { name: "Briefingを読み込み中…" }),
      ).toBeVisible();

      await page.keyboard.press("Escape");
      await expect(sheet).toHaveCount(0);
      await expect(trigger).toBeFocused();

      gate.release();
      await gate.settle();
    } finally {
      if (gate.blocked()) gate.release();
      await gate.remove();
    }

    await expect(page).toHaveURL(/\/briefing(?:\?|$)/);
    await trigger.click();
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        }),
    );
    await expect(sheet).toBeVisible();
    await expect(
      sheet.getByRole("status", { name: "Briefingを読み込み中…" }),
    ).toHaveCount(0);
  });

  test("desktop: article cardから記事detailへのroute response待機で旧画面、status、overlayを保つ", async ({
    page,
  }) => {
    await page.goto("/");
    const articleCard = page.locator('a[href^="/news/"]').first();
    await expect(articleCard).toBeVisible();
    const href = await articleCard.getAttribute("href");
    const pathname = targetPathname(href, "/news/");

    const gate = await holdNavigationRsc(page, pathname);
    try {
      await articleCard.click();
      await page.waitForTimeout(250);

      expect(gate.blocked()).toBe(true);
      await expect(articleCard).toBeVisible();
      await expect(
        page.getByRole("status", { name: "記事を読み込み中…" }),
      ).toBeVisible({ timeout: 100 });
      await expect(page.getByTestId("page-navigation-overlay")).toBeVisible({
        timeout: 100,
      });
    } finally {
      if (gate.blocked()) {
        gate.release();
        await gate.settle();
      }
      await gate.remove();
    }

    await expect(page).toHaveURL((url) => url.pathname === pathname);
    await expect(
      page.getByRole("status", { name: "記事を読み込み中…" }),
    ).toHaveCount(0);
  });

  test("desktop: Briefing cardからdetailへのroute response待機で旧画面、status、overlayを保つ", async ({
    page,
  }) => {
    await page.goto("/briefing");
    const briefingCard = page.locator('a[href^="/briefing/"]').first();
    await expect(briefingCard).toBeVisible();
    const href = await briefingCard.getAttribute("href");
    const pathname = targetPathname(href, "/briefing/");

    const gate = await holdNavigationRsc(page, pathname);
    try {
      await briefingCard.click();
      await page.waitForTimeout(250);

      expect(gate.blocked()).toBe(true);
      await expect(briefingCard).toBeVisible();
      await expect(
        page.getByRole("status", { name: "Briefingを読み込み中…" }),
      ).toBeVisible({ timeout: 100 });
      await expect(page.getByTestId("page-navigation-overlay")).toBeVisible({
        timeout: 100,
      });
    } finally {
      if (gate.blocked()) {
        gate.release();
        await gate.settle();
      }
      await gate.remove();
    }

    await expect(page).toHaveURL((url) => url.pathname === pathname);
    await expect(
      page.getByRole("status", { name: "Briefingを読み込み中…" }),
    ).toHaveCount(0);
  });
});
