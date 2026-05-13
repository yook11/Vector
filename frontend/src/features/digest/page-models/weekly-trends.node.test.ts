import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWeeklyTrends: vi.fn(),
}));

vi.mock("../api/get-weekly-trends", () => ({
  getWeeklyTrends: mocks.getWeeklyTrends,
}));

import { getWeeklyTrendsViewModel } from "./weekly-trends";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("getWeeklyTrendsViewModel", () => {
  it("empty state は state='empty' で透過する", async () => {
    mocks.getWeeklyTrends.mockResolvedValue({ state: "empty" });
    const result = await getWeeklyTrendsViewModel();
    expect(result).toEqual({ state: "empty" });
  });

  it("ready state は categories 等のフィールドを保持して透過する", async () => {
    const ready = {
      state: "ready" as const,
      windowStart: "2026-04-26",
      windowEnd: "2026-05-03",
      sourceAnalysisCount: 42,
      categories: [
        {
          categoryId: 1,
          categoryName: "AI",
          trendingEntities: [],
          newEntities: [],
        },
      ],
    };
    mocks.getWeeklyTrends.mockResolvedValue(ready);
    const result = await getWeeklyTrendsViewModel();
    expect(result).toEqual(ready);
  });

  it("getWeeklyTrends を 1 度だけ呼ぶ", async () => {
    mocks.getWeeklyTrends.mockResolvedValue({ state: "empty" });
    await getWeeklyTrendsViewModel();
    expect(mocks.getWeeklyTrends).toHaveBeenCalledTimes(1);
  });
});
