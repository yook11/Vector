import { delay, HttpResponse, http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { server } from "@/test/msw/server";
import { ApiError } from "./error";
import { requestEmpty, requestJson } from "./fetcher";

// msw の handler は test 内で `server.use(...)` で都度定義する。
// グローバル handler を持たないことで features 横断 mock 禁止と整合させる。

const TEST_URL = "http://test.local/items";

describe("requestJson — msw 経由の HTTP 経路", () => {
  it("200 + JSON body を T としてパースして返す", async () => {
    server.use(
      http.get(TEST_URL, () => HttpResponse.json({ id: 1, name: "alpha" })),
    );
    const result = await requestJson<{ id: number; name: string }>(TEST_URL);
    expect(result).toEqual({ id: 1, name: "alpha" });
  });

  it("FastAPI HTTPException (string detail) を ApiError に整形して throw", async () => {
    server.use(
      http.get(TEST_URL, () =>
        HttpResponse.json({ detail: "Resource not found" }, { status: 404 }),
      ),
    );
    await expect(requestJson(TEST_URL)).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      detail: "Resource not found",
    });
  });

  it("Pydantic validation array detail を 'field: msg' に正規化して throw", async () => {
    server.use(
      http.post(TEST_URL, () =>
        HttpResponse.json(
          {
            detail: [
              {
                loc: ["body", "email"],
                msg: "invalid format",
                type: "value_error",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    await expect(
      requestJson(TEST_URL, { method: "POST" }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      detail: "email: invalid format",
    });
  });

  it("解析不能 body は statusText フォールバックで ApiError(status, statusText)", async () => {
    server.use(
      http.get(
        TEST_URL,
        () =>
          new HttpResponse("oops", {
            status: 500,
            statusText: "Internal Server Error",
          }),
      ),
    );
    const error = await requestJson(TEST_URL).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(500);
    // body は plain text で normalizeErrorDetail() が "" を返すので statusText に fallback
    expect((error as ApiError).detail).toBe("Internal Server Error");
  });
});

describe("requestEmpty — 204 No Content", () => {
  it("204 で void resolve (body を読まない)", async () => {
    server.use(
      http.delete(TEST_URL, () => new HttpResponse(null, { status: 204 })),
    );
    const result = await requestEmpty(TEST_URL, { method: "DELETE" });
    expect(result).toBeUndefined();
  });
});

// timeout / AbortSignal 系は実時間 10s を待たないために fake timers を使う。
// msw の `delay(Infinity)` で hung response を作り、`vi.advanceTimersByTime`
// で REQUEST_TIMEOUT_MS (10_000ms) を一気に進めて AbortController.abort を
// 発火させる idiom。AbortSignal.any も実装に合わせ自然に動く。
describe("fetcher — timeout / AbortSignal 系", () => {
  afterEach(() => {
    // 外部 abort で fetch が reject した後も 10s setTimeout が pending 残り、
    // real timer 復帰時に発火して unhandled rejection を起こす。test ごとに
    // pending timer をクリアしてから real timer に戻す。
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("backend hang の場合、10s 経過で ApiError(408) timeout に正規化", async () => {
    server.use(
      http.get(TEST_URL, async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    // Promise / queueMicrotask は実時間で進める必要があるため shouldAdvanceTime
    // を有効化し、setTimeout だけを virtual time でジャンプさせる。
    vi.useFakeTimers({ shouldAdvanceTime: true });

    const promise = requestJson(TEST_URL);
    // ノードの unhandledRejection 判定を回避するため、reject 観測用の catch
    // ハンドラを advanceTimersByTimeAsync より先に attach する。
    const errorPromise = promise.catch((e: unknown) => e);
    // 10_000ms 経過で AbortController.abort() が発火
    await vi.advanceTimersByTimeAsync(10_000);

    const error = await errorPromise;
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(408);
    expect((error as ApiError).detail).toMatch(/timeout/i);
    expect((error as ApiError).detail).toContain("10000ms");
  });

  it("外部 AbortSignal による abort は AbortError をそのまま透過 (408 に正規化しない)", async () => {
    server.use(
      http.get(TEST_URL, async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    // fake timers にしておかないと、内部 10s setTimeout が real timer で
    // 走り続け、test 終了後に発火して unhandled rejection を起こす。
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const externalController = new AbortController();
    const promise = requestJson(TEST_URL, {
      signal: externalController.signal,
    });
    // 外部 signal が起点なので timeoutController.signal.aborted は false の
    // まま、ApiError(408) には変換されず AbortError がそのまま伝搬する。
    externalController.abort();

    const error = await promise.catch((e: unknown) => e);
    // jsdom 上で DOMException は Error subclass にならない場合があるので
    // name で判定する (実装の `err instanceof Error && err.name === "AbortError"`
    // 経路は fetch が thrown する Error 系を期待していて、その意味で `.name`
    // を verify することが本質)。
    expect((error as Error).name).toBe("AbortError");
    // ApiError には絶対に正規化されない (caller が自分の abort を解釈する)
    expect(error).not.toBeInstanceOf(ApiError);
  });

  it("AbortSignal.any: 外部 signal と timeout signal の両 OR で fetch を中断 (timeout より先に外部 abort)", async () => {
    // タイミング検証: 外部 signal が **timeout より先に** abort された場合、
    // AbortSignal.any で merge した signal は即時 aborted=true になり、fetch
    // は外部由来として AbortError 透過 (408 化しない)。
    server.use(
      http.get(TEST_URL, async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    vi.useFakeTimers({ shouldAdvanceTime: true });
    const externalController = new AbortController();
    const promise = requestJson(TEST_URL, {
      signal: externalController.signal,
    });

    // 5s 経過 (timeout 10s より前) で外部 abort
    await vi.advanceTimersByTimeAsync(5_000);
    externalController.abort();

    const error = await promise.catch((e: unknown) => e);
    // jsdom の DOMException は環境により Error subclass 認識が分かれるので、
    // name で判定する。ApiError でないことが本質。
    expect((error as Error).name).toBe("AbortError");
    expect(error).not.toBeInstanceOf(ApiError);
  });

  it("backend が 408 を返した場合は通常の ApiError(408, statusText) として扱う (timeout 由来と同じ status だが経路が違う)", async () => {
    // 経路の違いを明示: timeout 経路 (上の test) は AbortError → 408 に
    // 正規化、backend の 408 レスポンスはそのまま ApiError(408, detail or
    // statusText) として伝搬する。両者は detail 文言で区別可能。
    server.use(
      http.get(
        TEST_URL,
        () =>
          new HttpResponse(null, {
            status: 408,
            statusText: "Request Timeout",
          }),
      ),
    );

    const error = await requestJson(TEST_URL).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(408);
    // backend 由来の場合は statusText fallback で "Request Timeout" が入り、
    // timeout 経路の "Request timeout after 10000ms" とは文字列上区別できる。
    expect((error as ApiError).detail).toBe("Request Timeout");
  });
});
