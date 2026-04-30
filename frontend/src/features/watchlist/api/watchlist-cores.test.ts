import { describe, expect, it, vi } from "vitest";
import type { typedServer } from "@/lib/api/typed-server-fetcher";

// `typed-server-fetcher` は `import "server-only"` と `requireEnv` を持つため、
// 何もせず import すると test 環境で throw する。core が実際に使うのは
// `apiVoid` のみで、これは純関数 (`fetcher.POST(...)` 由来 promise を await して
// 戻り値を捨てる) なので簡易な passthrough mock で十分。`typedServer` は core
// が引数 DI で受け取るので mock 側で undefined のままで構わない。
vi.mock("@/lib/api/typed-server-fetcher", () => ({
  typedServer: undefined,
  apiVoid: async (p: Promise<unknown>) => {
    await p;
  },
}));

import { addToWatchlistCore, removeFromWatchlistCore } from "./watchlist-cores";

// `typedServer` (= openapi-fetch Client) の最小 mock。core が使う POST / DELETE
// のみ実装し、戻り値は `apiVoid` が満たす shape (`{ response: Response }`) で OK。
type FetcherMock = {
  POST: ReturnType<typeof vi.fn>;
  DELETE: ReturnType<typeof vi.fn>;
};

const okResponse = (status = 204) =>
  Promise.resolve({
    data: undefined,
    response: new Response(null, { status }),
  });

const buildFetcher = (overrides?: Partial<FetcherMock>): FetcherMock => ({
  POST: vi.fn().mockReturnValue(okResponse(204)),
  DELETE: vi.fn().mockReturnValue(okResponse(204)),
  ...overrides,
});

const asTypedServer = (fetcher: FetcherMock) =>
  fetcher as unknown as typeof typedServer;

describe("addToWatchlistCore", () => {
  it("typedServer.POST を `/api/v1/me/watchlist` + body { articleId } で呼ぶ", async () => {
    const fetcher = buildFetcher();
    await addToWatchlistCore(123, asTypedServer(fetcher));

    expect(fetcher.POST).toHaveBeenCalledTimes(1);
    expect(fetcher.POST).toHaveBeenCalledWith("/api/v1/me/watchlist", {
      body: { articleId: 123 },
    });
  });

  it("articleId の数値をそのまま渡す (coerce しない)", async () => {
    const fetcher = buildFetcher();
    await addToWatchlistCore(0, asTypedServer(fetcher));
    expect(fetcher.POST).toHaveBeenCalledWith("/api/v1/me/watchlist", {
      body: { articleId: 0 },
    });
  });

  it("fetcher の reject を伝搬する", async () => {
    const error = new Error("Conflict");
    const fetcher = buildFetcher({
      POST: vi.fn().mockRejectedValue(error),
    });
    await expect(addToWatchlistCore(1, asTypedServer(fetcher))).rejects.toBe(
      error,
    );
  });
});

describe("removeFromWatchlistCore", () => {
  it("typedServer.DELETE を `/api/v1/me/watchlist/{article_id}` + path-param で呼ぶ", async () => {
    const fetcher = buildFetcher();
    await removeFromWatchlistCore(456, asTypedServer(fetcher));

    expect(fetcher.DELETE).toHaveBeenCalledTimes(1);
    expect(fetcher.DELETE).toHaveBeenCalledWith(
      "/api/v1/me/watchlist/{article_id}",
      { params: { path: { article_id: 456 } } },
    );
  });

  it("body は付かない (DELETE)", async () => {
    const fetcher = buildFetcher();
    await removeFromWatchlistCore(1, asTypedServer(fetcher));
    const init = fetcher.DELETE.mock.calls[0]?.[1] as { body?: unknown };
    expect(init.body).toBeUndefined();
  });

  it("fetcher の reject を伝搬する", async () => {
    const error = new Error("Not Found");
    const fetcher = buildFetcher({
      DELETE: vi.fn().mockRejectedValue(error),
    });
    await expect(
      removeFromWatchlistCore(1, asTypedServer(fetcher)),
    ).rejects.toBe(error);
  });
});
