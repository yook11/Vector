import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getBriefing: vi.fn(),
}));

vi.mock("../api/get-briefing", () => ({
  getBriefing: mocks.getBriefing,
}));

import { getBriefingDetailViewModel } from "./briefing-detail";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("getBriefingDetailViewModel", () => {
  it("ready 状態を透過する", async () => {
    const ready = {
      state: "ready" as const,
      weekStart: "2026-04-20",
      generatedAt: "2026-04-27T00:05:00+09:00",
      modelName: "deepseek-v4-pro",
      inputArticleCount: 132,
      category: { id: 1, slug: "ai", name: "AI" },
      headline: "今週の AI",
      overview: "今週の AI 業界の流れ",
      keyArticles: [],
      watchPoints: [],
      articles: [],
    };
    mocks.getBriefing.mockResolvedValue(ready);
    const result = await getBriefingDetailViewModel("ai");
    expect(result).toEqual(ready);
  });

  it("empty 状態を透過する", async () => {
    const empty = {
      state: "empty" as const,
      category: { id: 1, slug: "ai", name: "AI" },
    };
    mocks.getBriefing.mockResolvedValue(empty);
    const result = await getBriefingDetailViewModel("ai");
    expect(result).toEqual(empty);
  });

  it("slug を api 関数に渡す", async () => {
    mocks.getBriefing.mockResolvedValue({
      state: "empty",
      category: { id: 1, slug: "robotics", name: "ロボティクス" },
    });
    await getBriefingDetailViewModel("robotics");
    expect(mocks.getBriefing).toHaveBeenCalledWith("robotics");
  });
});
