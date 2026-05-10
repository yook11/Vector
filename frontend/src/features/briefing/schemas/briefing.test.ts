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
      overview: "今週は LLM 推論コスト削減と...",
      stories: [{ takeaway: "記事から読み取った内容", articleIds: [1, 2] }],
      articles: [
        { id: 1, titleJa: "記事1", sourceName: "TechCrunch", url: "https://x" },
      ],
    });
    expect(result.success).toBe(true);
    if (result.success && result.data.state === "ready") {
      expect(result.data.overview).toBe("今週は LLM 推論コスト削減と...");
      expect(result.data.stories.length).toBe(1);
      expect(result.data.stories[0]?.takeaway).toBe("記事から読み取った内容");
      expect(result.data.articles[0]?.titleJa).toBe("記事1");
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

  it("rejects ready missing required fields (e.g. overview)", () => {
    const result = BriefingResponseSchema.safeParse({
      state: "ready",
      weekStart: "2026-04-20",
      generatedAt: "2026-04-27T00:05:00+09:00",
      modelName: "deepseek-v4-pro",
      inputArticleCount: 0,
      category: CATEGORY,
      headline: "h",
      stories: [],
      articles: [],
    });
    expect(result.success).toBe(false);
  });
});
