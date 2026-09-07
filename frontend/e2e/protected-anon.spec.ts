import { readFileSync } from "node:fs";
import {
  type APIRequestContext,
  expect,
  request,
  test,
} from "@playwright/test";
import { USER } from "./fixtures/users";

// 公開内容の取得成功と、個人・管理データの認証境界を検証する。

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const USER_STATE = "e2e/.auth/user.json";
const ADMIN_STATE = "e2e/.auth/admin.json";

const STATIC_PROTECTED_PATHS = [
  "/watchlist",
  "/research",
  "/settings",
  "/admin/users/new",
] as const;

interface StorageState {
  cookies: { name: string; value: string }[];
}

/**
 * proxy の名前ベース session 判定を通る fake cookie 用に、cookie 名だけを読む。
 */
function sessionCookieName(storagePath: string): string {
  const state = JSON.parse(readFileSync(storagePath, "utf8")) as StorageState;
  const cookie = state.cookies.find((c) => c.name.includes("session_token"));
  if (!cookie) {
    throw new Error(`session cookie not found in ${storagePath}`);
  }
  return cookie.name;
}

// setup project が storageState を書いた後に読むため、test 実行時まで遅延する。
let cachedCookieName: string | undefined;
function fakeCookieHeader(): string {
  cachedCookieName ??= sessionCookieName(USER_STATE);
  return `${cachedCookieName}=invalid-e2e-fake-session`;
}

function authedContext(storagePath: string): Promise<APIRequestContext> {
  return request.newContext({ baseURL: BASE, storageState: storagePath });
}

/**
 * in-stream redirect と HTTP redirect の両方で fake-cookie payload leak を検出する。
 */
async function expectNoLeak(path: string, sentinels: string[]): Promise<void> {
  const fake = await request.newContext({
    baseURL: BASE,
    extraHTTPHeaders: { cookie: fakeCookieHeader() },
  });
  try {
    const immediateBody = await (
      await fake.get(path, { maxRedirects: 0 })
    ).text();
    const followedBody = await (await fake.get(path)).text();
    for (const sentinel of sentinels) {
      expect(
        immediateBody,
        `immediate fake-cookie body of ${path} leaks: ${sentinel}`,
      ).not.toContain(sentinel);
      expect(
        followedBody,
        `followed fake-cookie body of ${path} leaks: ${sentinel}`,
      ).not.toContain(sentinel);
    }
  } finally {
    await fake.dispose();
  }
}

async function expectPublic(path: string, sentinels: string[]): Promise<void> {
  for (const fake of [false, true]) {
    const context = await request.newContext({
      baseURL: BASE,
      extraHTTPHeaders: fake ? { cookie: fakeCookieHeader() } : {},
    });
    try {
      for (const rsc of [false, true]) {
        const response = await context.get(path, {
          headers: rsc ? { RSC: "1" } : {},
          maxRedirects: 0,
        });
        expect(response.status()).toBe(200);
        const body = await response.text();
        expect(body).not.toContain("NEXT_REDIRECT");
        if (!rsc)
          for (const sentinel of sentinels) expect(body).toContain(sentinel);
      }
    } finally {
      await context.dispose();
    }
  }
}

test.describe("protected route auth boundary (anon / fake-cookie)", () => {
  test("`/` dashboard は未認証で記事カタログを閲覧できる (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/")).text();
      const link = body.match(/\/news\/\d+/)?.[0];
      test.skip(
        !link,
        "seed に記事が無く /news/<id> link が出ないため検証不能",
      );
      await expectPublic("/", [link as string]);
    } finally {
      await user.dispose();
    }
  });

  test("`/news/<id>` 記事詳細は未認証で本文/title を閲覧できる (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const home = await (await user.get("/")).text();
      const id = home.match(/\/news\/(\d+)/)?.[1];
      test.skip(!id, "seed に記事が無いため検証不能");
      const path = `/news/${id}`;
      const body = await (await user.get(path)).text();

      const sentinels = ["ダッシュボードに戻る"];
      const articleTitle = body
        .match(/<title>(.*?)<\/title>/)?.[1]
        ?.replace(/\s*\|\s*Vector$/, "")
        .trim();
      if (
        articleTitle &&
        articleTitle !== "Vector" &&
        !articleTitle.includes("Not Found")
      ) {
        sentinels.push(articleTitle);
      }
      for (const sentinel of sentinels) {
        expect(body, `authed ${path} should contain: ${sentinel}`).toContain(
          sentinel,
        );
      }
      await expectPublic(path, sentinels);
    } finally {
      await user.dispose();
    }
  });

  test("`/briefing` 一覧は未認証で週次データを閲覧できる (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/briefing")).text();
      const sentinel = "今週のブリーフィング";
      test.skip(
        !body.includes(sentinel),
        "briefing 一覧 gated content が出ないため検証不能",
      );
      await expectPublic("/briefing", [sentinel]);
    } finally {
      await user.dispose();
    }
  });

  test("`/briefing/<slug>` 詳細は未認証で解説を閲覧できる (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const list = await (await user.get("/briefing")).text();
      const slug = list.match(/href="\/briefing\/([a-z0-9-]+)"/i)?.[1];
      test.skip(!slug, "briefing slug が無いため検証不能");
      const path = `/briefing/${slug}`;
      const body = await (await user.get(path)).text();
      const sentinel = "一覧に戻る";
      expect(body).toContain(sentinel);
      await expectPublic(path, [sentinel]);
    } finally {
      await user.dispose();
    }
  });

  test("`/trends` は未認証でトレンドを閲覧できる (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/trends")).text();
      const dataMarker = "件の記事から集計";
      const emptyMarker = "該当するワードはありません";
      const sentinel = body.includes(dataMarker)
        ? dataMarker
        : body.includes(emptyMarker)
          ? emptyMarker
          : null;
      test.skip(!sentinel, "trends gated content が出ないため検証不能");
      await expectPublic("/trends", [sentinel as string]);
    } finally {
      await user.dispose();
    }
  });

  test("`/watchlist` は未認証 payload に保存記事を漏らさない (guard)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/watchlist")).text();
      const emptyMarker = "ウォッチした記事がありません";
      expect(body).toContain("ウォッチリスト");
      const sentinel = body.includes(emptyMarker)
        ? emptyMarker
        : "ウォッチリストを読み込み中…";
      await expectNoLeak("/watchlist", [sentinel as string]);
    } finally {
      await user.dispose();
    }
  });

  test("`/settings` (admin) は未認証 payload に source を漏らさない (guard)", async () => {
    // user.json は非 admin で / へ redirect されるため positive control は admin。
    const admin = await authedContext(ADMIN_STATE);
    try {
      const body = await (await admin.get("/settings")).text();
      const populated = "Endpoint URL";
      const empty = "No sources configured";
      const sentinel = body.includes(populated)
        ? populated
        : body.includes(empty)
          ? empty
          : null;
      test.skip(!sentinel, "settings gated content が出ないため検証不能");
      await expectNoLeak("/settings", [sentinel as string]);
    } finally {
      await admin.dispose();
    }
  });

  test("未認証 (cookie 無し) は全 protected route で /auth/login へ redirect", async () => {
    const anon = await request.newContext({ baseURL: BASE });
    try {
      for (const path of STATIC_PROTECTED_PATHS) {
        const res = await anon.get(path, { maxRedirects: 0 });
        expect([302, 303, 307, 308], `${path} should proxy-redirect`).toContain(
          res.status(),
        );
        expect(
          res.headers().location ?? "",
          `${path} should redirect to /auth/login`,
        ).toContain("/auth/login");
      }
    } finally {
      await anon.dispose();
    }
  });
});

test("公開ページから個人機能へ移動し、ログイン後に元のクエリへ戻る", async ({
  browser,
}) => {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.goto("/");
    await expect(page.locator('a[href^="/news/"]').first()).toBeVisible();
    await page.locator('a[href="/research"]').first().click();
    await expect(page).toHaveURL(/\/auth\/login\?callbackUrl=/);
    await page.goto("/watchlist?page=2");
    await expect(page).toHaveURL(/callbackUrl=%2Fwatchlist%3Fpage%3D2/);
    await page.getByLabel("メールアドレス").fill(USER.email);
    await page.getByLabel("パスワード").fill(USER.password);
    const loginResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/auth/sign-in/email") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "ログイン", exact: true }).click();
    expect((await loginResponse).status()).toBe(200);
    await expect(page).toHaveURL(/\/watchlist\?page=2$/);
  } finally {
    await context.close();
  }
});

test("偽Cookieでもエージェントの履歴画面へは進めない", async ({ browser }) => {
  const context = await browser.newContext();
  try {
    await context.addCookies([
      {
        name: sessionCookieName(USER_STATE),
        value: "invalid-e2e-fake-session",
        url: BASE,
      },
    ]);
    const page = await context.newPage();
    await page.goto("/research?view=history");
    await expect(page).toHaveURL(
      /\/auth\/login\?callbackUrl=%2Fresearch%3Fview%3Dhistory/,
    );
    await expect(
      page.getByRole("button", { name: "ログイン", exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});

test("一般ユーザーは管理画面を利用できない", async ({ browser }) => {
  const context = await browser.newContext({ storageState: USER_STATE });
  try {
    const page = await context.newPage();
    await page.goto("/settings");
    await expect(page).toHaveURL(`${BASE}/`);
    await page.goto("/admin/users/new");
    await expect(page).toHaveURL(`${BASE}/`);
  } finally {
    await context.close();
  }
});

test("公開記事の保存操作は未ログインならログインへ誘導する", async ({
  browser,
}) => {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.goto("/");
    const article = page.locator('a[href^="/news/"]').first();
    const href = await article.getAttribute("href");
    expect(href).toBeTruthy();
    await article.click();
    await expect(page).toHaveURL(new URL(href ?? "/", BASE).href);
    await expect(
      page.getByRole("link", { name: "ダッシュボードに戻る", exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Add to watchlist", exact: true })
      .first()
      .click();
    await expect(page).toHaveURL(/\/auth\/login\?callbackUrl=/);
    expect(new URL(page.url()).searchParams.get("callbackUrl")).toBe(href);
  } finally {
    await context.close();
  }
});

test("外部URLを復帰先に指定してもログイン後はサイト内に留まる", async ({
  browser,
}) => {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const externalRequests: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).hostname === "callback-target.invalid")
        externalRequests.push(request.url());
    });
    await page.goto(
      `/auth/login?callbackUrl=${encodeURIComponent("https://callback-target.invalid/path")}`,
    );
    await page.getByLabel("メールアドレス").fill(USER.email);
    await page.getByLabel("パスワード").fill(USER.password);
    const loginResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/auth/sign-in/email") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "ログイン", exact: true }).click();
    expect((await loginResponse).status()).toBe(200);
    await expect(page).toHaveURL(`${BASE}/`);
    await expect(page.locator('a[href^="/news/"]').first()).toBeVisible();
    expect(externalRequests).toEqual([]);
  } finally {
    await context.close();
  }
});
