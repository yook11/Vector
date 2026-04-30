import { delay, HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "@/test/msw/server";

// `vi.mock` factory は file 先頭に hoisted されるので、その中で参照する変数も
// `vi.hoisted` で持ち上げる必要がある。
const mocks = vi.hoisted(() => ({
  buildAuth: vi.fn(async () => ({ Authorization: "Bearer test-token" })),
  getSession: vi.fn(),
}));

// `server-only` は client bundle 混入を防ぐ sentinel。test では noop に差し替える。
vi.mock("server-only", () => ({}));

// `internal-config` は module load 時に `requireEnv("INTERNAL_API_URL")` /
// `requireEnv("INTERNAL_API_SECRET")` を呼ぶ。test 環境で env が無いと throw
// するため、必要な export を提供する mock を用意する。
vi.mock("@/lib/api/internal-config", () => ({
  INTERNAL_API_URL: "http://test.local/api/v1",
  buildInternalAuthHeaders: mocks.buildAuth,
}));

// auth guard は Better Auth + DB が要るので test では mock。test ごとに session
// の有無を切り替えられるようにする。
vi.mock("@/lib/auth/guards", () => ({
  getCurrentSession: mocks.getSession,
}));

import { ApiError } from "./error";
import {
  apiCall,
  apiVoid,
  typedPublic,
  typedServer,
} from "./typed-server-fetcher";

beforeEach(() => {
  vi.clearAllMocks();
  // デフォルトは authed user。session が無いことを検証する test だけ上書きする。
  mocks.getSession.mockResolvedValue({
    user: { id: "u1", role: "user" },
  });
});

describe("typedServer.GET — response 型導出 + path-param", () => {
  it("path-param を埋め込み response data を返す", async () => {
    server.use(
      http.get("http://test.local/api/v1/articles/:id", ({ params }) =>
        HttpResponse.json({
          id: Number(params.id),
          translatedTitle: "Hello",
        }),
      ),
    );

    const data = await apiCall(
      typedServer.GET("/api/v1/articles/{article_id}", {
        params: { path: { article_id: 42 } },
      }),
    );

    // 型導出が効いていれば data.id は number として narrow されている。
    expect(data.id).toBe(42);
    expect(data.translatedTitle).toBe("Hello");
  });

  it("Authorization header に HS256 JWT を付与する", async () => {
    let receivedAuth: string | null = null;
    server.use(
      http.get("http://test.local/api/v1/me/watchlist/ids", ({ request }) => {
        receivedAuth = request.headers.get("authorization");
        return HttpResponse.json({ ids: [1, 2, 3] });
      }),
    );

    await apiCall(typedServer.GET("/api/v1/me/watchlist/ids", {}));

    expect(mocks.buildAuth).toHaveBeenCalledTimes(1);
    expect(receivedAuth).toBe("Bearer test-token");
  });

  it("session が無い場合は Authorization を付けない", async () => {
    mocks.getSession.mockResolvedValue(null);
    let receivedAuth: string | null = null;
    server.use(
      http.get("http://test.local/api/v1/me/watchlist/ids", ({ request }) => {
        receivedAuth = request.headers.get("authorization");
        return HttpResponse.json({ ids: [] });
      }),
    );

    await apiCall(typedServer.GET("/api/v1/me/watchlist/ids", {}));

    expect(mocks.buildAuth).not.toHaveBeenCalled();
    expect(receivedAuth).toBeNull();
  });

  it("Next.js cache options (next.tags) を fetch init に素通しする", async () => {
    // openapi-fetch は init を fetch にそのまま渡すので、tags は次の RequestInit
    // 拡張で乗る。msw 経由では Request object 化された後の蓋を見られないため、
    // 200 レスポンスが正常にハンドルされること (= タグ付きでも経路が壊れない)
    // のみ smoke で検証する。
    server.use(
      http.get("http://test.local/api/v1/me/watchlist/ids", () =>
        HttpResponse.json({ ids: [42] }),
      ),
    );

    const data = await apiCall(
      typedServer.GET("/api/v1/me/watchlist/ids", {
        next: { tags: ["watchlist:me"] },
      }),
    );

    expect(data.ids).toEqual([42]);
  });
});

describe("typedServer.POST + apiVoid — 204 No Content 経路", () => {
  it("body を JSON で送り、204 を void に整形する", async () => {
    let receivedBody: unknown = null;
    server.use(
      http.post(
        "http://test.local/api/v1/me/watchlist",
        async ({ request }) => {
          receivedBody = await request.json();
          return new HttpResponse(null, { status: 204 });
        },
      ),
    );

    const result = await apiVoid(
      typedServer.POST("/api/v1/me/watchlist", {
        body: { articleId: 42 },
      }),
    );

    expect(result).toBeUndefined();
    expect(receivedBody).toEqual({ articleId: 42 });
  });

  it("DELETE で 204 を返した場合も void", async () => {
    server.use(
      http.delete(
        "http://test.local/api/v1/me/watchlist/:id",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );

    const result = await apiVoid(
      typedServer.DELETE("/api/v1/me/watchlist/{article_id}", {
        params: { path: { article_id: 7 } },
      }),
    );

    expect(result).toBeUndefined();
  });
});

describe("error normalization", () => {
  it("4xx の HTTPException string detail を ApiError に整形して throw", async () => {
    server.use(
      http.get("http://test.local/api/v1/articles/:id", () =>
        HttpResponse.json({ detail: "Article not found" }, { status: 404 }),
      ),
    );

    await expect(
      apiCall(
        typedServer.GET("/api/v1/articles/{article_id}", {
          params: { path: { article_id: 999 } },
        }),
      ),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      detail: "Article not found",
    });
  });

  it("422 の Pydantic validation array を 'field: msg' に整形", async () => {
    server.use(
      http.post("http://test.local/api/v1/me/watchlist", () =>
        HttpResponse.json(
          {
            detail: [
              {
                loc: ["body", "articleId"],
                msg: "must be positive",
                type: "value_error",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );

    await expect(
      apiVoid(
        typedServer.POST("/api/v1/me/watchlist", {
          body: { articleId: -1 },
        }),
      ),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      detail: "articleId: must be positive",
    });
  });

  it("解析不能 body は statusText フォールバックで ApiError(status, statusText)", async () => {
    server.use(
      http.get(
        "http://test.local/api/v1/articles/:id",
        () =>
          new HttpResponse("oops", {
            status: 500,
            statusText: "Internal Server Error",
          }),
      ),
    );

    const error = await apiCall(
      typedServer.GET("/api/v1/articles/{article_id}", {
        params: { path: { article_id: 1 } },
      }),
    ).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(500);
    expect((error as ApiError).detail).toBe("Internal Server Error");
  });
});

describe("timeout / AbortSignal", () => {
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("backend hang の場合、10s 経過で ApiError(408) timeout に正規化", async () => {
    server.use(
      http.get("http://test.local/api/v1/articles/:id", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    vi.useFakeTimers({ shouldAdvanceTime: true });
    const promise = apiCall(
      typedServer.GET("/api/v1/articles/{article_id}", {
        params: { path: { article_id: 1 } },
      }),
    );
    const errorPromise = promise.catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(10_000);

    const error = await errorPromise;
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(408);
    expect((error as ApiError).detail).toMatch(/timeout/i);
    expect((error as ApiError).detail).toContain("10000ms");
  });
});

describe("apiCall / apiVoid — defensive error 分岐", () => {
  // errorMiddleware を通らない経路 (= 直接 `{ error, response }` shape を渡された場合)
  // でも ApiError に正規化することを保証する。実際の openapi-fetch flow では
  // errorMiddleware が non-ok を全 throw するのでここに来ることは無いが、
  // defense-in-depth として残している経路の回帰検出。
  it("apiCall: { error, response } shape を ApiError に正規化", async () => {
    const fakePromise = Promise.resolve({
      error: { detail: "Forbidden" },
      response: new Response(null, { status: 403, statusText: "Forbidden" }),
    });

    await expect(apiCall(fakePromise)).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      detail: "Forbidden",
    });
  });

  it("apiVoid: { error, response } shape を ApiError に正規化", async () => {
    const fakePromise = Promise.resolve({
      error: { detail: "Conflict" },
      response: new Response(null, { status: 409, statusText: "Conflict" }),
    });

    await expect(apiVoid(fakePromise)).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      detail: "Conflict",
    });
  });
});

describe("typedPublic — auth なし", () => {
  it("session があっても Authorization は付けない", async () => {
    let receivedAuth: string | null = null;
    server.use(
      http.get("http://test.local/api/v1/categories", ({ request }) => {
        receivedAuth = request.headers.get("authorization");
        return HttpResponse.json([]);
      }),
    );

    await apiCall(typedPublic.GET("/api/v1/categories", {}));

    expect(mocks.buildAuth).not.toHaveBeenCalled();
    expect(receivedAuth).toBeNull();
  });

  it("public でも 4xx は ApiError 経路", async () => {
    server.use(
      http.get("http://test.local/api/v1/categories", () =>
        HttpResponse.json({ detail: "Service unavailable" }, { status: 503 }),
      ),
    );

    await expect(
      apiCall(typedPublic.GET("/api/v1/categories", {})),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      detail: "Service unavailable",
    });
  });
});
