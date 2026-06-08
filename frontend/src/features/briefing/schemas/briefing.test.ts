import { describe, expect, it } from "vitest";
import { BriefingListResponseSchema, BriefingResponseSchema } from "./briefing";

const CATEGORY = { id: 1, slug: "ai", name: "AI" };

describe("BriefingListResponseSchema", () => {
  it("accepts items with both ready (latest object) and empty (latest=null)", () => {
    const result = BriefingListResponseSchema.safeParse({
      currentWeekStart: "2026-04-27",
      items: [
        {
          category: CATEGORY,
          latest: {
            weekStart: "2026-04-20",
            headline: "今週の AI ハイライト",
          },
        },
        {
          category: { id: 2, slug: "robotics", name: "ロボティクス" },
          latest: null,
        },
      ],
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.items[0]?.latest?.headline).toBe(
        "今週の AI ハイライト",
      );
      expect(result.data.items[1]?.latest).toBeNull();
    }
  });

  it("rejects when currentWeekStart is not an ISO date", () => {
    const result = BriefingListResponseSchema.safeParse({
      currentWeekStart: "2026-04-27T00:00:00Z",
      items: [],
    });
    expect(result.success).toBe(false);
  });

  it("rejects when latest is missing entirely (must be present as null or object)", () => {
    const result = BriefingListResponseSchema.safeParse({
      currentWeekStart: "2026-04-27",
      items: [{ category: CATEGORY }],
    });
    expect(result.success).toBe(false);
  });
});

describe("BriefingResponseSchema", () => {
  it("narrows to ready when state='ready' and required fields present", () => {
    const result = BriefingResponseSchema.safeParse({
      state: "ready",
      weekStart: "2026-04-20",
      generatedAt: "2026-04-27T00:05:00+09:00",
      modelName: "deepseek-v4-pro",
      inputArticleCount: 132,
      category: CATEGORY,
      headline: "今週の AI ハイライト",
      summary: "今週の総括リード",
      chapters: [
        { heading: "資金とインフラ", body: "今週は LLM 推論コスト削減と..." },
      ],
      keyArticles: [{ articleId: 1, significance: "なぜ重要か" }],
      watchPoints: [{ statement: "今後どこを見るべきか" }],
      articles: [
        {
          id: 1,
          titleJa: "記事1",
          sourceName: "TechCrunch",
          url: "https://x",
          publishedAt: "2026-04-19T09:00:00+09:00",
        },
        {
          id: 2,
          titleJa: "記事2",
          sourceName: "Hacker News",
          url: "https://y",
          publishedAt: null,
        },
      ],
    });
    expect(result.success).toBe(true);
    if (result.success && result.data.state === "ready") {
      expect(result.data.summary).toBe("今週の総括リード");
      expect(result.data.chapters[0]?.heading).toBe("資金とインフラ");
      expect(result.data.chapters[0]?.body).toBe(
        "今週は LLM 推論コスト削減と...",
      );
      expect(result.data.keyArticles.length).toBe(1);
      expect(result.data.keyArticles[0]?.articleId).toBe(1);
      expect(result.data.keyArticles[0]?.significance).toBe("なぜ重要か");
      expect(result.data.watchPoints[0]?.statement).toBe(
        "今後どこを見るべきか",
      );
      expect(result.data.articles[0]?.titleJa).toBe("記事1");
      expect(result.data.articles[0]?.publishedAt).toBe(
        "2026-04-19T09:00:00+09:00",
      );
      expect(result.data.articles[1]?.publishedAt).toBeNull();
    }
  });

  it("narrows to empty when state='empty'", () => {
    const result = BriefingResponseSchema.safeParse({
      state: "empty",
      category: CATEGORY,
    });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.state).toBe("empty");
  });

  it("rejects unknown state value", () => {
    const result = BriefingResponseSchema.safeParse({
      state: "loading",
      category: CATEGORY,
    });
    expect(result.success).toBe(false);
  });

  it("rejects ready missing required fields (e.g. summary/chapters)", () => {
    const result = BriefingResponseSchema.safeParse({
      state: "ready",
      weekStart: "2026-04-20",
      generatedAt: "2026-04-27T00:05:00+09:00",
      modelName: "deepseek-v4-pro",
      inputArticleCount: 0,
      category: CATEGORY,
      headline: "h",
      keyArticles: [],
      watchPoints: [],
      articles: [],
    });
    expect(result.success).toBe(false);
  });
});
