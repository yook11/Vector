import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listBriefings: vi.fn(),
}));

vi.mock("../api/list-briefings", () => ({
  listBriefings: mocks.listBriefings,
}));

import { getBriefingListViewModel } from "./briefing-list";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("getBriefingListViewModel", () => {
  it("ready/empty 混在の items を currentWeekStart と一緒に透過する", async () => {
    const fixture = {
      currentWeekStart: "2026-04-27",
      items: [
        {
          category: { id: 1, slug: "ai", name: "AI" },
          latest: { weekStart: "2026-04-20", headline: "AI 動向" },
        },
        {
          category: { id: 2, slug: "robotics", name: "ロボティクス" },
          latest: null,
        },
      ],
    };
    mocks.listBriefings.mockResolvedValue(fixture);
    const result = await getBriefingListViewModel();
    expect(result).toEqual(fixture);
  });

  it("listBriefings を 1 度だけ呼ぶ", async () => {
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-04-27",
      items: [],
    });
    await getBriefingListViewModel();
    expect(mocks.listBriefings).toHaveBeenCalledTimes(1);
  });
});
