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
  it("未生成の null はそのまま透過する", async () => {
    mocks.getTrends.mockResolvedValue(null);
    const result = await getTrendsViewModel();
    expect(result).toBeNull();
  });

  it("トレンドは categoryTrends 等のフィールドを保持して透過する", async () => {
    const trends = {
      week: { start: "2026-04-26", end: "2026-05-02" },
      previousWeek: { start: "2026-04-19", end: "2026-04-25" },
      generatedAt: "2026-05-03T06:00:00Z",
      analyzedArticleCount: 42,
      categoryTrends: [
        {
          category: { slug: "ai", name: "AI" },
          mentionTrends: [
            {
              name: "NVIDIA",
              type: "company" as const,
              articleVolume: { count: 30, previousWeekCount: 5, rank: 1 },
              growth: { rate: 5.0, rank: 1 },
              keyPoints: [],
              mentionedWith: [],
            },
          ],
        },
      ],
    };
    mocks.getTrends.mockResolvedValue(trends);
    const result = await getTrendsViewModel();
    expect(result).toEqual(trends);
  });

  it("getTrends を 1 度だけ呼ぶ", async () => {
    mocks.getTrends.mockResolvedValue(null);
    await getTrendsViewModel();
    expect(mocks.getTrends).toHaveBeenCalledTimes(1);
  });
});
