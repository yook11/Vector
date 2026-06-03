import { beforeEach, describe, expect, it, vi } from "vitest";
import type { getPipelineHealth as getPipelineHealthSdk } from "@/types/sdk.gen";
import type { PipelineHealthResponse } from "@/types/types.gen";

// `hey-api-interceptors` は `import "server-only"` を持ち、import 副作用で
// singleton client に interceptor を attach する。テストでは引数 DI した fetcher
// だけを使うため空 mock で抑止する。
vi.mock("server-only", () => ({}));
vi.mock("@/lib/api/hey-api-interceptors", () => ({}));
// sdk.gen を import すると client.gen → hey-api.config が INTERNAL_API_URL を
// 要求するため、生成 SDK を mock して module 評価を断つ。default 引数で使われる
// getPipelineHealth もこの mock に解決される。
const hoisted = vi.hoisted(() => ({ defaultSdk: vi.fn() }));
vi.mock("@/types/sdk.gen", () => ({ getPipelineHealth: hoisted.defaultSdk }));

import { getPipelineStatus } from "./get-pipeline-status";

const sample: PipelineHealthResponse = {
  summary: {
    failedEventCount24h: 0,
    backfillTargetTotal: 0,
    oldestBackfillTargetAgeSeconds: null,
    completionQueueCount: 0,
    oldestCompletionQueueAgeSeconds: null,
    observedAt: "2026-06-03T00:00:00Z",
    eventWindowStart: "2026-06-02T00:00:00Z",
  },
  stages: [],
};

// hey-api SDK 関数が返す形 (`{ data, response }`) を満たす最小 mock。
const okResponse = <T>(data: T, status = 200) =>
  Promise.resolve({
    data,
    response: new Response(null, { status }),
  });

beforeEach(() => {
  vi.clearAllMocks();
});

describe("getPipelineStatus", () => {
  it("引数 fetcher を throwOnError + cache no-store で呼ぶ", async () => {
    const fn = vi.fn().mockResolvedValue(okResponse(sample));
    const result = await getPipelineStatus(
      fn as unknown as typeof getPipelineHealthSdk,
    );

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith({ throwOnError: true, cache: "no-store" });
    expect(result).toBe(sample);
  });

  it("default では生成 SDK getPipelineHealth を使う", async () => {
    hoisted.defaultSdk.mockResolvedValue(okResponse(sample));
    const result = await getPipelineStatus();

    expect(hoisted.defaultSdk).toHaveBeenCalledWith({
      throwOnError: true,
      cache: "no-store",
    });
    expect(result).toBe(sample);
  });

  it("propagates fetcher rejections", async () => {
    const error = new Error("Forbidden");
    const fn = vi.fn().mockRejectedValue(error);
    await expect(
      getPipelineStatus(fn as unknown as typeof getPipelineHealthSdk),
    ).rejects.toBe(error);
  });
});
