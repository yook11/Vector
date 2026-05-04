import { describe, expect, it, vi } from "vitest";
import type {
  addToWatchlist as addToWatchlistSdk,
  removeFromWatchlist as removeFromWatchlistSdk,
} from "@/types/sdk.gen";

// `hey-api-interceptors` は `import "server-only"` を持ち、import するだけで
// publicClient を生成 + interceptor を attach する副作用がある。テスト環境では
// auth/error 経路を実行しないので、空 mock で抑止する。
vi.mock("server-only", () => ({}));
vi.mock("@/lib/api/hey-api-interceptors", () => ({}));

import { addToWatchlistCore, removeFromWatchlistCore } from "./watchlist-cores";

// hey-api SDK 関数が返す形 (`{ data, response }`) を満たす最小 mock。
const okResponse = (status = 204) =>
  Promise.resolve({
    data: undefined,
    response: new Response(null, { status }),
  });

describe("addToWatchlistCore", () => {
  it("addToWatchlist sdk fn を body { articleId } + throwOnError で呼ぶ", async () => {
    const fn = vi.fn().mockReturnValue(okResponse(204));
    await addToWatchlistCore(123, fn as unknown as typeof addToWatchlistSdk);

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      body: { articleId: 123 },
    });
  });

  it("articleId の数値をそのまま渡す (coerce しない)", async () => {
    const fn = vi.fn().mockReturnValue(okResponse(204));
    await addToWatchlistCore(0, fn as unknown as typeof addToWatchlistSdk);
    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      body: { articleId: 0 },
    });
  });

  it("fetcher の reject を伝搬する", async () => {
    const error = new Error("Conflict");
    const fn = vi.fn().mockRejectedValue(error);
    await expect(
      addToWatchlistCore(1, fn as unknown as typeof addToWatchlistSdk),
    ).rejects.toBe(error);
  });
});

describe("removeFromWatchlistCore", () => {
  it("removeFromWatchlist sdk fn を path { article_id } + throwOnError で呼ぶ", async () => {
    const fn = vi.fn().mockReturnValue(okResponse(204));
    await removeFromWatchlistCore(
      456,
      fn as unknown as typeof removeFromWatchlistSdk,
    );

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      path: { article_id: 456 },
    });
  });

  it("body は付かない (DELETE)", async () => {
    const fn = vi.fn().mockReturnValue(okResponse(204));
    await removeFromWatchlistCore(
      1,
      fn as unknown as typeof removeFromWatchlistSdk,
    );
    const opts = fn.mock.calls[0]?.[0] as { body?: unknown };
    expect(opts.body).toBeUndefined();
  });

  it("fetcher の reject を伝搬する", async () => {
    const error = new Error("Not Found");
    const fn = vi.fn().mockRejectedValue(error);
    await expect(
      removeFromWatchlistCore(
        1,
        fn as unknown as typeof removeFromWatchlistSdk,
      ),
    ).rejects.toBe(error);
  });
});
