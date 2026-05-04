import { describe, expect, it, vi } from "vitest";
import type { typedServer } from "@/lib/api/typed-server-fetcher";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types";

// `typed-server-fetcher` は `import "server-only"` を持つため、何もせず import
// すると test 環境で throw する。core が実際に使うのは `apiCall` / `apiVoid` で、
// これらは純関数 (`fetcher.X(...)` 由来 promise を await して `data` を unwrap)
// なので簡易な passthrough mock で十分。`typedServer` は core が引数 DI で
// 受け取るので mock 側で undefined のままで構わない。
vi.mock("@/lib/api/typed-server-fetcher", () => ({
  typedServer: undefined,
  apiCall: async <T>(p: Promise<{ data?: T }>) => {
    const r = await p;
    return r.data as T;
  },
  apiVoid: async (p: Promise<unknown>) => {
    await p;
  },
}));

import {
  activateSourceCore,
  createSourceCore,
  deactivateSourceCore,
  deleteSourceCore,
} from "./source-cores";

const sampleDetail: NewsSourceDetail = {
  id: 1,
  name: "Example",
  sourceType: "rss",
  siteUrl: "https://example.com",
  endpointUrl: "https://example.com/feed",
  isActive: true,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
};

// `typedServer` (= openapi-fetch Client) の最小 mock。core が使う PATCH / POST
// / DELETE のみ実装。戻り値は `apiCall` / `apiVoid` が満たす shape
// (`{ data, response }`) に揃える。
type FetcherMock = {
  PATCH: ReturnType<typeof vi.fn>;
  POST: ReturnType<typeof vi.fn>;
  DELETE: ReturnType<typeof vi.fn>;
};

const okResponse = <T>(data: T, status = 200) =>
  Promise.resolve({
    data,
    response: new Response(null, { status }),
  });

const buildFetcher = (overrides?: Partial<FetcherMock>): FetcherMock => ({
  PATCH: vi.fn().mockReturnValue(okResponse(sampleDetail)),
  POST: vi.fn().mockReturnValue(okResponse(sampleDetail, 201)),
  DELETE: vi.fn().mockReturnValue(okResponse(undefined, 204)),
  ...overrides,
});

const asTypedServer = (fetcher: FetcherMock) =>
  fetcher as unknown as typeof typedServer;

describe("activateSourceCore", () => {
  it("typedServer.PATCH を `/api/v1/admin/sources/{source_id}/activate` + path-param で呼ぶ", async () => {
    const fetcher = buildFetcher();
    const result = await activateSourceCore(42, asTypedServer(fetcher));

    expect(fetcher.PATCH).toHaveBeenCalledTimes(1);
    expect(fetcher.PATCH).toHaveBeenCalledWith(
      "/api/v1/admin/sources/{source_id}/activate",
      { params: { path: { source_id: 42 } } },
    );
    expect(result).toBe(sampleDetail);
  });

  it("propagates fetcher rejections", async () => {
    const error = new Error("Forbidden");
    const fetcher = buildFetcher({
      PATCH: vi.fn().mockRejectedValue(error),
    });
    await expect(activateSourceCore(1, asTypedServer(fetcher))).rejects.toBe(
      error,
    );
  });
});

describe("deactivateSourceCore", () => {
  it("typedServer.PATCH を `/api/v1/admin/sources/{source_id}/deactivate` + path-param で呼ぶ", async () => {
    const fetcher = buildFetcher();
    const result = await deactivateSourceCore(7, asTypedServer(fetcher));

    expect(fetcher.PATCH).toHaveBeenCalledWith(
      "/api/v1/admin/sources/{source_id}/deactivate",
      { params: { path: { source_id: 7 } } },
    );
    expect(result).toBe(sampleDetail);
  });

  it("uses the exact id without coercion", async () => {
    const fetcher = buildFetcher();
    await deactivateSourceCore(0, asTypedServer(fetcher));
    expect(fetcher.PATCH).toHaveBeenCalledWith(
      "/api/v1/admin/sources/{source_id}/deactivate",
      { params: { path: { source_id: 0 } } },
    );
  });
});

describe("createSourceCore", () => {
  const body: NewsSourceCreate = {
    name: "New Source",
    sourceType: "rss",
    siteUrl: "https://new.example.com",
    endpointUrl: "https://new.example.com/feed",
  };

  it("typedServer.POST を `/api/v1/admin/sources` + body で呼ぶ", async () => {
    const fetcher = buildFetcher();
    const result = await createSourceCore(body, asTypedServer(fetcher));

    expect(fetcher.POST).toHaveBeenCalledTimes(1);
    expect(fetcher.POST).toHaveBeenCalledWith("/api/v1/admin/sources", {
      body,
    });
    expect(result).toBe(sampleDetail);
  });

  it("propagates fetcher errors", async () => {
    const error = new Error("Bad Request");
    const fetcher = buildFetcher({
      POST: vi.fn().mockRejectedValue(error),
    });
    await expect(createSourceCore(body, asTypedServer(fetcher))).rejects.toBe(
      error,
    );
  });
});

describe("deleteSourceCore", () => {
  it("typedServer.DELETE を `/api/v1/admin/sources/{source_id}` + path-param で呼ぶ", async () => {
    const fetcher = buildFetcher();
    const result = await deleteSourceCore(99, asTypedServer(fetcher));

    expect(fetcher.DELETE).toHaveBeenCalledWith(
      "/api/v1/admin/sources/{source_id}",
      { params: { path: { source_id: 99 } } },
    );
    expect(result).toBeUndefined();
  });

  it("propagates fetcher errors", async () => {
    const error = new Error("Not Found");
    const fetcher = buildFetcher({
      DELETE: vi.fn().mockRejectedValue(error),
    });
    await expect(deleteSourceCore(99, asTypedServer(fetcher))).rejects.toBe(
      error,
    );
  });
});
