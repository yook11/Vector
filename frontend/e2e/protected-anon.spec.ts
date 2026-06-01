import { readFileSync } from "node:fs";
import {
  type APIRequestContext,
  expect,
  request,
  test,
} from "@playwright/test";

/**
 * protected route の認証境界回帰テスト。本番ログで観測した PPR の static shell
 * 漏洩 (未認証 / fake session cookie で `'use cache'` データが payload に乗る) を
 * 凍結する発見的 oracle。専用 `auth-boundary` project から実行される
 * (playwright.config.ts)。既存 e2e と同様 backend + dev server 起動前提の
 * ローカル only (CI 非搭載)。
 *
 * 各 path で 3 段:
 *   1. positive control: 認証済み context で GET し、保護データ由来の sentinel が
 *      body に「含まれる」ことを確認 (seed 空なら skip して負 assertion の空振りを防ぐ)。
 *   2. leak 検出: proxy を通る fake session cookie (名前は実 cookie から、値は無効)
 *      で GET し、maxRedirects:0 の即時 body と redirect 追従後 body の双方に
 *      sentinel が「含まれない」ことを確認。修正前は section が gate されず data を
 *      render して RED、修正後は section 先頭の requireSession が redirect して GREEN。
 *   3. proxy redirect: cookie 無しで maxRedirects:0 → 3xx かつ Location が
 *      /auth/login を含むことを確認 (proxy 層の一次関門)。
 *
 * grade:
 *   - strong (`/`, `/news/<id>`, `/briefing`, `/briefing/<slug>`, `/weekly-trends`):
 *     publicClient + `'use cache'` を anon でも render するため、修正前後で
 *     RED→GREEN が flip する本丸。
 *   - guard (`/watchlist`, `/settings`): authed client 経由で anon は 401 と
 *     既に fail-closed。flip は保証しないが「未認証で保護データが出ない」不変条件を凍結。
 */

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const USER_STATE = "e2e/.auth/user.json";
const ADMIN_STATE = "e2e/.auth/admin.json";

// no-cookie proxy redirect の網羅対象 (param route は本文中で discover する)。
const STATIC_PROTECTED_PATHS = [
  "/",
  "/briefing",
  "/weekly-trends",
  "/watchlist",
  "/settings",
] as const;

interface StorageState {
  cookies: { name: string; value: string }[];
}

/**
 * storageState から better-auth session cookie の「名前だけ」を取り出す。
 * proxy の getSessionCookie は cookie 名の存在のみ見る (値検証なし) ため、
 * 名前さえ合致すれば fake cookie で proxy を通過できる。値 (秘匿) は読まない。
 */
function sessionCookieName(storagePath: string): string {
  const state = JSON.parse(readFileSync(storagePath, "utf8")) as StorageState;
  const cookie = state.cookies.find((c) => c.name.includes("session_token"));
  if (!cookie) {
    throw new Error(`session cookie not found in ${storagePath}`);
  }
  return cookie.name;
}

// cookie 名は setup project が user.json を書いた後に解決する。module load 時に
// 読むと Playwright の collection (= setup 実行より前) で fresh checkout や
// clean な e2e/.auth が ENOENT を投げ、dependencies:["setup"] では救えず spec
// 全体を巻き込んで落ちる。test 実行時まで遅延し、初回だけ読んで memoize する。
let cachedCookieName: string | undefined;
function fakeCookieHeader(): string {
  cachedCookieName ??= sessionCookieName(USER_STATE);
  return `${cachedCookieName}=invalid-e2e-fake-session`;
}

function authedContext(storagePath: string): Promise<APIRequestContext> {
  return request.newContext({ baseURL: BASE, storageState: storagePath });
}

/**
 * authed body 由来の sentinel が、fake-cookie の即時 body (maxRedirects:0) と
 * redirect 追従後 body の双方に出ないことを assert する。in-stream redirect
 * (200 + skeleton) でも HTTP 307 でも、どちらに data が乗っても捕捉できるよう
 * 双方を見る。
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
      // 記事カード由来の /news/<id> link が本丸の sentinel。
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

      // chrome (gated body が render された証拠) + title (塞いだ保護データ) を sentinel に。
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
      // positive control: sentinel が authed body に実在することを確認。
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
      // "今週:" は BriefingListContent (gated) のみが render する。
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
      // BackLink "← 一覧に戻る" は BriefingDetailContent (gated) のみが render する。
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

  test("`/weekly-trends` は未認証 payload にトレンドを漏らさない (strong)", async () => {
    const user = await authedContext(USER_STATE);
    try {
      const body = await (await user.get("/weekly-trends")).text();
      // gated content は data marker か empty marker のどちらかを必ず render する。
      const dataMarker = "件の分析を集計";
      const emptyMarker = "週次トレンドはまだ生成されていません";
      const sentinel = body.includes(dataMarker)
        ? dataMarker
        : body.includes(emptyMarker)
          ? emptyMarker
          : null;
      test.skip(!sentinel, "weekly-trends gated content が出ないため検証不能");
      await expectNoLeak("/weekly-trends", [sentinel as string]);
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
      // populated は table header、empty state は "No sources configured"。
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
