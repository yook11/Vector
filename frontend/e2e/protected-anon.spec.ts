import { readFileSync } from "node:fs";
import {
  type APIRequestContext,
  expect,
  request,
  test,
} from "@playwright/test";

/**
 * PPR static shell leak の回帰テスト。
 * fake session cookie が proxy を通っても protected payload を受け取れないことを保証する。
 * strong routes は publicClient + "use cache" 面、guard routes は fail-closed 面を凍結する。
 */

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const USER_STATE = "e2e/.auth/user.json";
const ADMIN_STATE = "e2e/.auth/admin.json";

const STATIC_PROTECTED_PATHS = [
  "/",
  "/briefing",
  "/trends",
  "/watchlist",
  "/settings",
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

test.describe("protected route auth boundary (anon / fake-cookie)", () => {
  test("`/` dashboard は未認証 payload に記事カタログを漏らさない (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/")).text();
      const link = body.match(/\/news\/\d+/)?.[0];
      test.skip(
        !link,
        "seed に記事が無く /news/<id> link が出ないため検証不能",
      );
      await expectNoLeak("/", [link as string]);
    } finally {
      await user.dispose();
    }
  });

  test("`/news/<id>` 記事詳細は未認証 payload に本文/title を漏らさない (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const home = await (await user.get("/")).text();
      const id = home.match(/\/news\/(\d+)/)?.[1];
      test.skip(!id, "seed に記事が無いため検証不能");
      const path = `/news/${id}`;
      const body = await (await user.get(path)).text();

      const sentinels = ["Back to Dashboard"];
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
      await expectNoLeak(path, sentinels);
    } finally {
      await user.dispose();
    }
  });

  test("`/briefing` 一覧は未認証 payload に週次データを漏らさない (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/briefing")).text();
      const sentinel = "今週:";
      test.skip(
        !body.includes(sentinel),
        "briefing 一覧 gated content が出ないため検証不能",
      );
      await expectNoLeak("/briefing", [sentinel]);
    } finally {
      await user.dispose();
    }
  });

  test("`/briefing/<slug>` 詳細は未認証 payload に解説を漏らさない (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const list = await (await user.get("/briefing")).text();
      const slug = list.match(/\/briefing\/([a-z0-9-]+)/i)?.[1];
      test.skip(!slug, "briefing slug が無いため検証不能");
      const path = `/briefing/${slug}`;
      const body = await (await user.get(path)).text();
      const sentinel = "← 一覧に戻る";
      test.skip(
        !body.includes(sentinel),
        "briefing 詳細 gated content が出ないため検証不能",
      );
      await expectNoLeak(path, [sentinel]);
    } finally {
      await user.dispose();
    }
  });

  test("`/trends` は未認証 payload にトレンドを漏らさない (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/trends")).text();
      const dataMarker = "件の記事から集計";
      const emptyMarker = "トレンドはまだ生成されていません";
      const sentinel = body.includes(dataMarker)
        ? dataMarker
        : body.includes(emptyMarker)
          ? emptyMarker
          : null;
      test.skip(!sentinel, "trends gated content が出ないため検証不能");
      await expectNoLeak("/trends", [sentinel as string]);
    } finally {
      await user.dispose();
    }
  });

  test("`/watchlist` は未認証 payload に保存記事を漏らさない (guard)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/watchlist")).text();
      const emptyMarker = "No saved articles";
      const link = body.match(/\/news\/\d+/)?.[0];
      const sentinel = body.includes(emptyMarker)
        ? emptyMarker
        : (link ?? null);
      test.skip(!sentinel, "watchlist gated content が出ないため検証不能");
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
