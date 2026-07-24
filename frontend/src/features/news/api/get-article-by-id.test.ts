import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/error";

const mocks = vi.hoisted(() => ({
  cacheLife: vi.fn(),
  getArticle: vi.fn(),
  publicClient: {},
}));

vi.mock("next/cache", () => ({ cacheLife: mocks.cacheLife }));
vi.mock("@/lib/api/hey-api-interceptors", () => ({
  publicClient: mocks.publicClient,
}));
vi.mock("@/types/sdk.gen", () => ({ getArticle: mocks.getArticle }));

import { getArticleById } from "./get-article-by-id";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("getArticleById", () => {
  it.each([
    404, 410,
  ])("HTTP %i はcache境界内でnullへ正規化する", async (status) => {
    mocks.getArticle.mockRejectedValue(
      new ApiError(status, "Article not found"),
    );

    await expect(getArticleById(42)).resolves.toBeNull();
    expect(mocks.cacheLife).toHaveBeenCalledWith("hours");
    expect(mocks.getArticle).toHaveBeenCalledWith({
      client: mocks.publicClient,
      throwOnError: true,
      path: { article_id: 42 },
    });
  });

  it("HTTP 500 はerror boundaryへ伝播する", async () => {
    const error = new ApiError(500, "Backend unavailable");
    mocks.getArticle.mockRejectedValue(error);

    await expect(getArticleById(42)).rejects.toBe(error);
  });
});
