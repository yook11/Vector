/** 本番Dockerイメージ・実認証DB・ローカルbackendで、CSP下の画面起動を検証する。 */
import { test as base, expect, type Page } from "@playwright/test";
import { USER } from "./fixtures/users";

const test = base.extend({
  page: async ({ page }, use) => {
    const errors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(() => {
      document.addEventListener("securitypolicyviolation", (event) => {
        console.error(
          `CSP violation: ${event.violatedDirective} ${event.blockedURI} ${event.sourceFile}:${event.lineNumber} ${event.sample}`,
        );
      });
    });
    await use(page);
    expect(errors).toEqual([]);
  },
});

async function signIn(page: Page) {
  const response = await page.request.post("/api/auth/sign-in/email", {
    data: USER,
  });
  expect(response.ok()).toBe(true);
}

test("画面からログインするとニュースにユーザーが表示される", async ({
  page,
}) => {
  await page.goto("/auth/login");
  await page.getByLabel("メールアドレス").fill(USER.email);
  await page.getByLabel("パスワード").fill(USER.password);
  await page.getByRole("button", { name: "ログイン", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText(USER.email, { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
});

test("ニュースからリサーチへ移動すると入力できる", async ({ page }) => {
  await signIn(page);
  await page.goto("/");
  await expect(page.getByText(USER.email, { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Research", exact: true }).click();
  await expect(page).toHaveURL(/\/research$/);
  await expect(page.locator("#research-question")).toBeEditable();
  await page.locator("#research-question").fill("半導体について調べたい");
  await expect(
    page.getByRole("button", { name: "送信", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("status", { name: "Researchを読み込み中…" }),
  ).toHaveCount(0);
});

test("リサーチの直接表示と再読み込み後にユーザーと入力欄が表示される", async ({
  page,
}) => {
  await signIn(page);
  await page.goto("/research");
  await expect(page.locator("#research-question")).toBeEditable();
  await page.reload();
  await expect(page.getByText(USER.email, { exact: true })).toBeVisible();
  await expect(page.locator("#research-question")).toBeEditable();
  await expect(
    page.getByRole("navigation", { name: "リサーチ履歴" }),
  ).toBeVisible();
});

test("本番レスポンスがnonce型CSPと起動スクリプトのSRIを維持する", async ({
  page,
}) => {
  await signIn(page);
  const response = await page.goto("/research");
  const policy = response?.headers()["content-security-policy"];
  expect(policy).toBeDefined();
  const scriptPolicy = policy
    ?.split(";")
    .find((directive) => directive.trim().startsWith("script-src "));
  expect(scriptPolicy).toContain("'nonce-");
  expect(scriptPolicy).not.toContain("'unsafe-inline'");
  expect(scriptPolicy).not.toContain("'unsafe-eval'");
  await expect(page.locator("#research-question")).toBeEditable();
  const integrity = await page
    .locator('script[src*="/webpack-"], script[src*="/main-app-"]')
    .evaluateAll((scripts) =>
      scripts.map((script) => script.getAttribute("integrity")),
    );
  expect(integrity).toHaveLength(2);
  expect(integrity.every((value) => value?.startsWith("sha256-"))).toBe(true);
});
