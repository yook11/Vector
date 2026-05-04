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
  getSession: vi.fn(),
}));

vi.mock("server-only", () => ({}));

vi.mock("@/lib/api/internal-config", () => ({
  INTERNAL_API_URL: "http://test.local/api/v1",
  buildInternalAuthHeaders: mocks.buildAuth,
}));

vi.mock("@/lib/auth/guards", () => ({
  getCurrentSession: mocks.getSession,
}));

import { client } from "@/types/client.gen";
import { ApiError } from "./error";
// 副作用 import で interceptor を登録する。実 production runtime では PR-H4a で
// 各 sdk call site が同様の side-effect import を行う想定。
import "./hey-api-interceptors";

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

    const response = new Response(null, {
      status: 404,
      statusText: "Not Found",
    });

    await expect(
      fn({ detail: "Article not found" }, response, {} as never),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      detail: "Article not found",
    });
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
  });
});
