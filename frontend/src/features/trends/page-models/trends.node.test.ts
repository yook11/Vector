import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getTrends: vi.fn(),
}));

vi.mock("../api/get-trends", () => ({
  getTrends: mocks.getTrends,
}));

import { getTrendsViewModel } from "./trends";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("getTrendsViewModel", () => {
  it("empty state は state='empty' で透過する", async () => {
    mocks.getTrends.mockResolvedValue({ state: "empty" });
    const result = await getTrendsViewModel();
    expect(result).toEqual({ state: "empty" });
  });

  it("trends state は categoryTrends 等のフィールドを保持して透過する", async () => {
    const trends = {
      state: "trends" as const,
      windowStart: "2026-04-26",
      windowEnd: "2026-05-03",
      generatedAt: "2026-05-03T06:00:00Z",
      sourceAnalysisCount: 42,
      categoryTrends: [
        {
          categoryId: 1,
          categorySlug: "ai",
          categoryName: "AI",
          mostMentioned: [
            {
              name: "NVIDIA",
              type: "company" as const,
              appearanceCount: 30,
              previousAppearanceCount: 5,
              growthRate: 5.0,
              keyPoints: [],
              relatedMentions: [],
            },
          ],
          fastestGrowing: [],
        },
      ],
    };
    mocks.getTrends.mockResolvedValue(trends);
    const result = await getTrendsViewModel();
    expect(result).toEqual(trends);
  });

  it("getTrends を 1 度だけ呼ぶ", async () => {
    mocks.getTrends.mockResolvedValue({ state: "empty" });
    await getTrendsViewModel();
    expect(mocks.getTrends).toHaveBeenCalledTimes(1);
  });
});
