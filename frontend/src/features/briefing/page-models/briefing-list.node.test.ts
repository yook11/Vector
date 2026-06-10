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

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

function makeLatest(
  overrides: {
    weekStart?: string;
    headline?: string;
    summary?: string;
    inputArticleCount?: number;
  } = {},
) {
  return {
    weekStart: overrides.weekStart ?? "2026-05-26",
    headline: overrides.headline ?? "見出し",
    summary: overrides.summary ?? "要約文",
    inputArticleCount: overrides.inputArticleCount ?? 10,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("getBriefingListViewModel — ready / pending split", () => {
  it("latest があるアイテムは ready に入り、latest===null は pending に入る", async () => {
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-06-02",
      totalArticles: 55,
      items: [
        {
          category: { slug: "ai", name: "AI" },
          latest: makeLatest({
            weekStart: "2026-06-02",
            headline: "AI の最前線",
            summary: "今週のAI動向まとめ",
            inputArticleCount: 12,
          }),
        },
        {
          category: { slug: "robotics", name: "ロボティクス" },
          latest: null,
        },
      ],
    });

    const result = await getBriefingListViewModel();

    // ready に latest ありアイテムが入る
    expect(result.ready).toHaveLength(1);
    const readyCard = result.ready[0]!;
    expect(readyCard.category).toEqual({ slug: "ai", name: "AI" });
    expect(readyCard.weekStart).toBe("2026-06-02");
    expect(readyCard.headline).toBe("AI の最前線");
    expect(readyCard.summary).toBe("今週のAI動向まとめ");
    expect(readyCard.inputArticleCount).toBe(12);

    // pending に latest なしアイテムが入る (slug / name のみ)
    expect(result.pending).toHaveLength(1);
    const pendingCat = result.pending[0]!;
    expect(pendingCat).toEqual({ slug: "robotics", name: "ロボティクス" });
  });

  it("全アイテムが ready のとき pending は空", async () => {
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-06-02",
      totalArticles: 20,
      items: [
        { category: { slug: "ai", name: "AI" }, latest: makeLatest() },
        {
          category: { slug: "bio", name: "バイオ" },
          latest: makeLatest({ headline: "バイオ速報" }),
        },
      ],
    });

    const result = await getBriefingListViewModel();

    expect(result.ready).toHaveLength(2);
    expect(result.pending).toHaveLength(0);
  });

  it("全アイテムが pending のとき ready は空", async () => {
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-06-02",
      totalArticles: 0,
      items: [
        {
          category: { slug: "ai", name: "AI" },
          latest: null,
        },
        {
          category: { slug: "bio", name: "バイオ" },
          latest: null,
        },
      ],
    });

    const result = await getBriefingListViewModel();

    expect(result.ready).toHaveLength(0);
    expect(result.pending).toHaveLength(2);
  });
});

describe("getBriefingListViewModel — ready の順序保持", () => {
  it("backend の item 順がそのまま ready の順になる (アルファベット順に並び替えない)", async () => {
    // 非アルファベット順で返ってきたとき、frontend は並び替えない
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-06-02",
      totalArticles: 30,
      items: [
        {
          category: { slug: "space", name: "スペース" },
          latest: makeLatest({ headline: "宇宙ニュース" }),
        },
        {
          category: { slug: "ai", name: "AI" },
          latest: makeLatest({ headline: "AI 動向" }),
        },
        {
          category: { slug: "bio", name: "バイオ" },
          latest: makeLatest({ headline: "バイオ速報" }),
        },
      ],
    });

    const result = await getBriefingListViewModel();

    expect(result.ready).toHaveLength(3);
    expect(result.ready.map((c) => c.category.slug)).toEqual([
      "space",
      "ai",
      "bio",
    ]);
    // headline でも確認 (二重チェック)
    expect(result.ready[0]!.headline).toBe("宇宙ニュース");
    expect(result.ready[1]!.headline).toBe("AI 動向");
    expect(result.ready[2]!.headline).toBe("バイオ速報");
  });
});

describe("getBriefingListViewModel — weekEnd 導出", () => {
  it("weekEnd は weekStart + 6 日 (月跨ぎ: 2026-05-26 → 2026-06-01)", async () => {
    // 仕様: weekEnd = weekStart + 6 日
    // 月跨ぎを選ぶことで addDaysIso の日付計算を非自明にする
    const weekStart = "2026-05-26";
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: weekStart,
      totalArticles: 0,
      items: [],
    });

    const result = await getBriefingListViewModel();

    // 仕様から直接導出: 2026-05-26 + 6 = 2026-06-01
    const expected = new Date(`${weekStart}T00:00:00Z`);
    expected.setUTCDate(expected.getUTCDate() + 6);
    const expectedStr = expected.toISOString().slice(0, 10);

    expect(result.weekEnd).toBe(expectedStr);
    expect(result.weekEnd).toBe("2026-06-01"); // 月跨ぎ確認
  });

  it("weekEnd は weekStart + 6 日 (年跨ぎ: 2025-12-29 → 2026-01-04)", async () => {
    const weekStart = "2025-12-29";
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: weekStart,
      totalArticles: 0,
      items: [],
    });

    const result = await getBriefingListViewModel();

    const expected = new Date(`${weekStart}T00:00:00Z`);
    expected.setUTCDate(expected.getUTCDate() + 6);
    const expectedStr = expected.toISOString().slice(0, 10);

    expect(result.weekEnd).toBe(expectedStr);
    expect(result.weekEnd).toBe("2026-01-04"); // 年跨ぎ確認
  });

  it("weekStart は currentWeekStart をそのまま返す", async () => {
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-06-02",
      totalArticles: 0,
      items: [],
    });

    const result = await getBriefingListViewModel();

    expect(result.weekStart).toBe("2026-06-02");
  });
});

describe("getBriefingListViewModel — totalArticles", () => {
  it("totalArticles はバックエンドの値をそのまま返す", async () => {
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-06-02",
      totalArticles: 137,
      items: [],
    });

    const result = await getBriefingListViewModel();

    expect(result.totalArticles).toBe(137);
  });

  it("totalArticles が 0 のとき 0 を返す", async () => {
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-06-02",
      totalArticles: 0,
      items: [],
    });

    const result = await getBriefingListViewModel();

    expect(result.totalArticles).toBe(0);
  });
});

describe("getBriefingListViewModel — listBriefings 呼び出し回数", () => {
  it("listBriefings を 1 度だけ呼ぶ", async () => {
    mocks.listBriefings.mockResolvedValue({
      currentWeekStart: "2026-06-02",
      totalArticles: 0,
      items: [],
    });

    await getBriefingListViewModel();

    expect(mocks.listBriefings).toHaveBeenCalledTimes(1);
  });
});
