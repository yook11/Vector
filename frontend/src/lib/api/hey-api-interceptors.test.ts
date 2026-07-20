/**
 * hey-api-interceptors の単体 test。実 client.request() を叩かず、
 * `client.interceptors.{request,error}.fns` に登録された fn を直接取り出して
 * mock options/response で実行する。
 *
 * `vi.hoisted` で持ち上げた mock を `vi.mock` factory で参照することで、
 * test ごとに session の有無 / auth 戻り値を切り替えられる。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  buildAuth: vi.fn(async () => ({ Authorization: "Bearer test-token" })),
  buildBff: vi.fn(async () => ({ Authorization: "Bearer bff-token" })),
  getSession: vi.fn(),
  logServerEvent: vi.fn(),
}));

vi.mock("server-only", () => ({}));

vi.mock("@/lib/api/internal-config", () => ({
  INTERNAL_API_URL: "http://test.local/api/v1",
  buildInternalAuthHeaders: mocks.buildAuth,
  buildBffRequestHeaders: mocks.buildBff,
}));

vi.mock("@/lib/auth/guards", () => ({
  getCurrentSession: mocks.getSession,
}));

vi.mock("@/lib/observability/server-log", () => ({
  logServerEvent: mocks.logServerEvent,
}));

import { client } from "@/types/client.gen";
import { ApiError, InternalFetchError } from "./error";
// 副作用 import で interceptor を登録する。実 production runtime では各 sdk call
// site が `import "@/lib/api/hey-api-interceptors"` または publicClient の named
// import 経由で module evaluation をトリガする。
import { publicClient } from "./hey-api-interceptors";

beforeEach(() => {
  vi.clearAllMocks();
  // デフォルトは authed user。session が無いケースだけ test 側で上書きする。
  mocks.getSession.mockResolvedValue({ user: { id: "u1", role: "user" } });
});

describe("hey-api request interceptor — auth header", () => {
  it("session があれば Authorization を注入する", async () => {
    const fn = client.interceptors.request.fns[0];
    if (!fn) throw new Error("request interceptor not registered");

    const headers = new Headers();
    // hey-api の request interceptor は ResolvedRequestOptions を受けるが、
    // fn が触るのは options.headers.set のみなので最低限の shape で足りる。
    await fn({ headers } as never);

    expect(mocks.buildAuth).toHaveBeenCalledTimes(1);
    expect(headers.get("Authorization")).toBe("Bearer test-token");
  });

  it("session が null なら header に触れない", async () => {
    mocks.getSession.mockResolvedValue(null);
    const fn = client.interceptors.request.fns[0];
    if (!fn) throw new Error("request interceptor not registered");

    const headers = new Headers();
    await fn({ headers } as never);

    expect(mocks.buildAuth).not.toHaveBeenCalled();
    expect(headers.has("Authorization")).toBe(false);
  });
});

describe("hey-api error interceptor — ApiError 正規化", () => {
  it("HTTPException string detail を ApiError に整形して throw", async () => {
    const fn = client.interceptors.error.fns[0];
    if (!fn) throw new Error("error interceptor not registered");

    const body = { detail: "Article not found" };
    const response = new Response(null, {
      status: 404,
      statusText: "Not Found",
    });

    const error = await (
      fn(body, response, {} as never) as Promise<unknown>
    ).catch((e: unknown) => e);

    expect(error).toMatchObject({
      name: "ApiError",
      status: 404,
      detail: "Article not found",
    });
    expect((error as ApiError).body).toBe(body);
    expect((error as ApiError).retryAfter).toBeNull();
    expect(mocks.logServerEvent).not.toHaveBeenCalled();
  });

  it("Pydantic validation array を 'field: msg' 形式に整形", async () => {
    const fn = client.interceptors.error.fns[0];
    if (!fn) throw new Error("error interceptor not registered");

    const response = new Response(null, { status: 422 });

    await expect(
      fn(
        {
          detail: [
            {
              loc: ["body", "articleId"],
              msg: "must be positive",
              type: "value_error",
            },
          ],
        },
        response,
        {} as never,
      ),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      detail: "articleId: must be positive",
    });
  });

  it("解析不能 body は statusText fallback で ApiError(status, statusText)", async () => {
    const fn = client.interceptors.error.fns[0];
    if (!fn) throw new Error("error interceptor not registered");

    const response = new Response(null, {
      status: 500,
      statusText: "Internal Server Error",
    });

    const error = await (
      fn("oops not json", response, {} as never) as Promise<unknown>
    ).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(500);
    expect((error as ApiError).detail).toBe("Internal Server Error");
  });

  it("response 不在 (network error) は status 0 + 'HTTP 0' fallback", async () => {
    const fn = client.interceptors.error.fns[0];
    if (!fn) throw new Error("error interceptor not registered");

    const error = await (
      fn("connection reset", undefined, {} as never) as Promise<unknown>
    ).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).detail).toBe("HTTP 0");
    expect(mocks.logServerEvent).not.toHaveBeenCalled();
  });

  it("InternalFetchError timeout は message を保持して構造化ログを出す", async () => {
    const fn = client.interceptors.error.fns[0];
    if (!fn) throw new Error("error interceptor not registered");

    const error = await (
      fn(
        new InternalFetchError("timeout", "Request timeout after 10000ms"),
        undefined,
        { method: "GET", url: "/api/v1/articles" } as never,
      ) as Promise<unknown>
    ).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).detail).toBe("Request timeout after 10000ms");
    expect((error as ApiError).meta).toMatchObject({
      kind: "timeout",
      method: "GET",
      path: "/api/v1/articles",
    });
    expect(mocks.logServerEvent).toHaveBeenCalledWith(
      "error",
      "frontend_internal_api_failure",
      {
        kind: "timeout",
        method: "GET",
        path: "/api/v1/articles",
        detail: "Request timeout after 10000ms",
      },
    );
  });

  it("InternalFetchError network は message を保持して構造化ログを出す", async () => {
    const fn = client.interceptors.error.fns[0];
    if (!fn) throw new Error("error interceptor not registered");

    const error = await (
      fn(new InternalFetchError("network", "fetch failed"), undefined, {
        method: "GET",
        url: "/api/v1/categories",
      } as never) as Promise<unknown>
    ).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).detail).toBe("fetch failed");
    expect((error as ApiError).meta).toMatchObject({
      kind: "network",
      path: "/api/v1/categories",
    });
    expect(mocks.logServerEvent).toHaveBeenCalledWith(
      "error",
      "frontend_internal_api_failure",
      {
        kind: "network",
        method: "GET",
        path: "/api/v1/categories",
        detail: "fetch failed",
      },
    );
  });

  it("HTTP 429 は warn の構造化ログを出す", async () => {
    const fn = client.interceptors.error.fns[0];
    if (!fn) throw new Error("error interceptor not registered");

    const body = {
      detail: "Daily research request limit exceeded",
      code: "research_daily_request_limit_exceeded",
      limit: 10,
      resetAt: "2026-07-21T00:00:00+09:00",
    };
    const response = new Response(null, {
      status: 429,
      statusText: "Too Many Requests",
      headers: { "Retry-After": "37" },
    });

    const error = await (
      fn(body, response, {
        method: "GET",
        url: "/api/v1/articles",
      } as never) as Promise<unknown>
    ).catch((e: unknown) => e);

    expect(error).toMatchObject({
      name: "ApiError",
      status: 429,
      detail: "Daily research request limit exceeded",
      meta: {
        kind: "http_429",
        method: "GET",
        path: "/api/v1/articles",
        status: 429,
      },
    });
    expect((error as ApiError).body).toBe(body);
    expect((error as ApiError).retryAfter).toBe("37");
    expect(mocks.logServerEvent).toHaveBeenCalledWith(
      "warn",
      "frontend_internal_api_failure",
      {
        kind: "http_429",
        method: "GET",
        path: "/api/v1/articles",
        status: 429,
        detail: "Daily research request limit exceeded",
      },
    );
  });

  it("HTTP 5xx は error の構造化ログを出す", async () => {
    const fn = client.interceptors.error.fns[0];
    if (!fn) throw new Error("error interceptor not registered");

    const response = new Response(null, {
      status: 500,
      statusText: "Internal Server Error",
    });

    await expect(
      fn("oops not json", response, {
        method: "GET",
        url: "/api/v1/articles",
      } as never),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 500,
      detail: "Internal Server Error",
      meta: {
        kind: "http_5xx",
        method: "GET",
        path: "/api/v1/articles",
        status: 500,
      },
    });
    expect(mocks.logServerEvent).toHaveBeenCalledWith(
      "error",
      "frontend_internal_api_failure",
      {
        kind: "http_5xx",
        method: "GET",
        path: "/api/v1/articles",
        status: 500,
        detail: "Internal Server Error",
      },
    );
  });
});

describe("publicClient — BFF 経由証明を付ける (session は読まない)", () => {
  it("request interceptor が user-less BFF JWT を注入する", async () => {
    const fn = publicClient.interceptors.request.fns[0];
    if (!fn) throw new Error("request interceptor not registered");

    const headers = new Headers();
    await fn({ headers } as never);

    expect(mocks.buildBff).toHaveBeenCalledTimes(1);
    expect(headers.get("Authorization")).toBe("Bearer bff-token");
  });

  it("getCurrentSession() を踏まない (cookies/headers 読取なし)", async () => {
    const fn = publicClient.interceptors.request.fns[0];
    if (!fn) throw new Error("request interceptor not registered");

    await fn({ headers: new Headers() } as never);

    expect(mocks.getSession).not.toHaveBeenCalled();
  });

  it("error interceptor は登録され ApiError を throw する", async () => {
    const fn = publicClient.interceptors.error.fns[0];
    if (!fn)
      throw new Error("error interceptor not registered on publicClient");

    const response = new Response(null, {
      status: 503,
      statusText: "Unavailable",
    });
    await expect(
      fn({ detail: "service down" }, response, {} as never),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      detail: "service down",
    });
  });
});

describe("runtime config — baseUrl が両 client に適用される", () => {
  // openapi-ts v0.97.1 の generated client.gen.ts は runtimeConfigPath を
  // wrap せず baseUrl 未設定で初期化するため、明示的に setConfig /
  // createClientConfig 経由で適用していることを test で固定する。
  it("singleton client に INTERNAL_API_URL の origin が baseUrl として set される", () => {
    expect(client.getConfig().baseUrl).toBe("http://test.local");
  });

  it("publicClient にも同じ baseUrl が適用される", () => {
    expect(publicClient.getConfig().baseUrl).toBe("http://test.local");
  });
});
