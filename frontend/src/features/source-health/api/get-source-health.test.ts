import { beforeEach, describe, expect, it, vi } from "vitest";
import type { getSourceHealth as getSourceHealthSdk } from "@/types/sdk.gen";
import type { SourceHealthResponse } from "@/types/types.gen";

// `hey-api-interceptors` は `import "server-only"` を持ち、import 副作用で
// singleton client に interceptor を attach する。テストでは引数 DI した fetcher
// だけを使うため空 mock で抑止する。
vi.mock("server-only", () => ({}));
vi.mock("@/lib/api/hey-api-interceptors", () => ({}));
// sdk.gen を import すると client.gen → hey-api.config が INTERNAL_API_URL を
// 要求するため、生成 SDK を mock して module 評価を断つ。default 引数で使われる
// getSourceHealth もこの mock に解決される。
const hoisted = vi.hoisted(() => ({ defaultSdk: vi.fn() }));
vi.mock("@/types/sdk.gen", () => ({ getSourceHealth: hoisted.defaultSdk }));

import { getSourceHealth } from "./get-source-health";

const sample: SourceHealthResponse = {
  windowHours: 24,
  observedAt: "2026-06-03T00:00:00Z",
  items: [],
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

describe("getSourceHealth", () => {
  it("引数 fetcher を throwOnError + no-store + windowHours query で呼ぶ", async () => {
    const fn = vi.fn().mockResolvedValue(okResponse(sample));
    const result = await getSourceHealth(
      24,
      fn as unknown as typeof getSourceHealthSdk,
    );

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      cache: "no-store",
      query: { windowHours: 24 },
    });
    expect(result).toBe(sample);
  });

  it("windowHours を query に透過する", async () => {
    const fn = vi.fn().mockResolvedValue(okResponse(sample));
    await getSourceHealth(168, fn as unknown as typeof getSourceHealthSdk);

    expect(fn).toHaveBeenCalledWith({
      throwOnError: true,
      cache: "no-store",
      query: { windowHours: 168 },
    });
  });

  it("default では生成 SDK getSourceHealth を使う", async () => {
    hoisted.defaultSdk.mockResolvedValue(okResponse(sample));
    const result = await getSourceHealth(48);

    expect(hoisted.defaultSdk).toHaveBeenCalledWith({
      throwOnError: true,
      cache: "no-store",
      query: { windowHours: 48 },
    });
    expect(result).toBe(sample);
  });

  it("propagates fetcher rejections", async () => {
    const error = new Error("Forbidden");
    const fn = vi.fn().mockRejectedValue(error);
    await expect(
      getSourceHealth(24, fn as unknown as typeof getSourceHealthSdk),
    ).rejects.toBe(error);
  });
});
