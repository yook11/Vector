import { describe, expect, it, vi } from "vitest";
import type {
  activateSource as activateSourceSdk,
  createNewsSource as createNewsSourceSdk,
  deactivateSource as deactivateSourceSdk,
  deleteNewsSource as deleteNewsSourceSdk,
} from "@/types/sdk.gen";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types/types.gen";

// `hey-api-interceptors` は `import "server-only"` を持ち、import するだけで
// publicClient を生成 + interceptor を attach する副作用がある。テスト環境では
// auth/error 経路を実行しないので、空 mock で抑止する。core が実際に使うのは
// 引数 DI で受け取る `fetcher` (sdk 関数) だけなので、interceptor 副作用は不要。
vi.mock("server-only", () => ({}));
vi.mock("@/lib/api/hey-api-interceptors", () => ({}));

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

// hey-api SDK 関数が返す形 (`{ data, response }`) を満たす最小 mock。
const okResponse = <T>(data: T, status = 200) =>
  Promise.resolve({
    data,
    response: new Response(null, { status }),
  });

describe("activateSourceCore", () => {
  it("activateSource sdk fn を path { source_id } + throwOnError で呼ぶ", async () => {
    const fn = vi.fn().mockResolvedValue(okResponse(sampleDetail));
    const result = await activateSourceCore(
      42,
      fn as unknown as typeof activateSourceSdk,
    );

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      path: { source_id: 42 },
    });
    expect(result).toBe(sampleDetail);
  });

  it("propagates fetcher rejections", async () => {
    const error = new Error("Forbidden");
    const fn = vi.fn().mockRejectedValue(error);
    await expect(
      activateSourceCore(1, fn as unknown as typeof activateSourceSdk),
    ).rejects.toBe(error);
  });
});

describe("deactivateSourceCore", () => {
  it("deactivateSource sdk fn を path { source_id } + throwOnError で呼ぶ", async () => {
    const fn = vi.fn().mockResolvedValue(okResponse(sampleDetail));
    const result = await deactivateSourceCore(
      7,
      fn as unknown as typeof deactivateSourceSdk,
    );

    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      path: { source_id: 7 },
    });
    expect(result).toBe(sampleDetail);
  });

  it("uses the exact id without coercion", async () => {
    const fn = vi.fn().mockResolvedValue(okResponse(sampleDetail));
    await deactivateSourceCore(0, fn as unknown as typeof deactivateSourceSdk);
    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      path: { source_id: 0 },
    });
  });
});

describe("createSourceCore", () => {
  const body: NewsSourceCreate = {
    name: "New Source",
    sourceType: "rss",
    siteUrl: "https://new.example.com",
    endpointUrl: "https://new.example.com/feed",
  };

  it("createNewsSource sdk fn を body + throwOnError で呼ぶ", async () => {
    const fn = vi.fn().mockResolvedValue(okResponse(sampleDetail, 201));
    const result = await createSourceCore(
      body,
      fn as unknown as typeof createNewsSourceSdk,
    );

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      body,
    });
    expect(result).toBe(sampleDetail);
  });

  it("propagates fetcher errors", async () => {
    const error = new Error("Bad Request");
    const fn = vi.fn().mockRejectedValue(error);
    await expect(
      createSourceCore(body, fn as unknown as typeof createNewsSourceSdk),
    ).rejects.toBe(error);
  });
});

describe("deleteSourceCore", () => {
  it("deleteNewsSource sdk fn を path { source_id } + throwOnError で呼ぶ", async () => {
    const fn = vi.fn().mockResolvedValue(okResponse(undefined, 204));
    const result = await deleteSourceCore(
      99,
      fn as unknown as typeof deleteNewsSourceSdk,
    );

    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      path: { source_id: 99 },
    });
    expect(result).toBeUndefined();
  });

  it("propagates fetcher errors", async () => {
    const error = new Error("Not Found");
    const fn = vi.fn().mockRejectedValue(error);
    await expect(
      deleteSourceCore(99, fn as unknown as typeof deleteNewsSourceSdk),
    ).rejects.toBe(error);
  });
});
