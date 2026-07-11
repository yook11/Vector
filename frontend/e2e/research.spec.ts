import { expect, test } from "@playwright/test";
import { RESEARCH_THREADS } from "./fixtures/research";

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
  const morePath = `/research/${RESEARCH_THREADS.A.id}?limit=3`;
  await page.goto(aPath);

  const main = page.getByRole("main");
  const aLink = page.locator(`a[href="${aPath}"]`);
  const bLink = page.locator(`a[href="${bPath}"]`);
  const moreLink = page.locator(`a[href="${morePath}"]`);
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
