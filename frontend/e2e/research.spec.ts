import { expect, type Locator, type Page, test } from "@playwright/test";
import {
  RESEARCH_HISTORY_LIMIT,
  RESEARCH_SOURCE_COUNT,
  RESEARCH_SOURCE_HREF,
  RESEARCH_THREADS,
} from "./fixtures/research";

const REQUIRED_VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 767, height: 900 },
  { width: 768, height: 900 },
  { width: 1023, height: 900 },
  { width: 1024, height: 768 },
  { width: 1440, height: 900 },
] as const;

function alphaPath(limit = RESEARCH_HISTORY_LIMIT, legacyView = false) {
  const view = legacyView ? "&view=sources" : "";
  return `/research/${RESEARCH_THREADS.A.id}?limit=${limit}${view}`;
}

function answerPanel(page: Page): Locator {
  return page
    .getByTestId("research-answer-slot")
    .first()
    .locator(
      "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' overflow-y-auto ')][1]",
    );
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
  return page.locator("form:has(textarea#research-question)");
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
    Number(moreUrl.searchParams.get("limit") ?? RESEARCH_HISTORY_LIMIT),
  ).toBe(RESEARCH_HISTORY_LIMIT);
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
      const sourcesTrigger = page.getByRole("button", { name: /ソース/ });
      const dock = composer(page);
      await expectScrollable(scroller);

      const inlineSources = page.getByRole("complementary", {
        name: "ソース",
      });
      if (viewport.width >= 1280) {
        await expect(inlineSources).toBeVisible();
      } else {
        await expect(inlineSources).toHaveCount(0);
      }
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

      if ((await inlineSources.count()) > 0) {
        const sourcesBox = await requiredBox(inlineSources);
        const composerRight = composerRailBox.x + composerRailBox.width;
        expect
          .soft(
            composerRight,
            `${viewport.width}px composer right=${composerRight}, inline sources left=${sourcesBox.x}`,
          )
          .toBeLessThanOrEqual(sourcesBox.x + 1);
      }

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
      const trigger = page.getByRole("button", { name: /ソース/ });
      const scroller = answerPanel(page);
      await textarea.fill(`sources-${width}`);
      await scroller.evaluate((element) => {
        element.scrollTop = 160;
      });
      const answerTop = await scroller.evaluate((element) => element.scrollTop);
      const composerBefore = await requiredBox(composer(page));

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

        await expectResearchHrefsWithoutView(page);
      } else {
        const inline = page.getByRole("complementary", { name: "ソース" });
        await expect(inline).toBeVisible();
        await expect(page.getByRole("dialog", { name: "ソース" })).toHaveCount(
          0,
        );
        const sourceScroller = inline.locator(".overflow-y-auto");
        await expect(
          sourceScroller.locator(`a[href="${RESEARCH_SOURCE_HREF}"]`),
        ).toBeVisible();
        await scrollByWheel(page, sourceScroller);
        expect(await scroller.evaluate((element) => element.scrollTop)).toBe(
          answerTop,
        );
        expectSameBox(await requiredBox(composer(page)), composerBefore);
        expect(await page.evaluate(() => window.scrollY)).toBe(0);
        await trigger.click();
        await expect(inline).toHaveCount(0);
        await expect(trigger).toBeFocused();
        await trigger.click();
        await expect(inline).toBeVisible();
        await expectResearchHrefsWithoutView(page);
      }

      await expect(textarea).toHaveValue(`sources-${width}`);
      expect(await scroller.evaluate((element) => element.scrollTop)).toBe(
        answerTop,
      );
      expectSameBox(await requiredBox(composer(page)), composerBefore);
      await expect(trigger).toContainText(String(RESEARCH_SOURCE_COUNT));
      await expect(page.getByRole("dialog", { name: "ソース" })).toHaveCount(0);
    });
  }
});
