import { describe, expect, it } from "vitest";
import { BriefingListResponseSchema, BriefingResponseSchema } from "./briefing";

const CATEGORY = { slug: "ai", name: "AI" };

const ARTICLE_EMBED = {
  id: 1,
  translatedTitle: "記事1",
  source: { name: "TechCrunch", attributionLabel: null },
  url: "https://x",
  publishedAt: "2026-04-19T09:00:00+09:00",
  keyPoints: ["要点A", "要点B"],
};

describe("BriefingListResponseSchema", () => {
  it("accepts items with both generated (latest object) and empty (latest=null)", () => {
    const result = BriefingListResponseSchema.safeParse({
      currentWeekStart: "2026-04-27",
      totalArticles: 64,
      items: [
        {
          category: CATEGORY,
          latest: {
            weekStart: "2026-04-20",
            headline: "今週の AI ハイライト",
            summary: "今週の総括リード",
            inputArticleCount: 64,
          },
        },
        {
          category: { slug: "robotics", name: "ロボティクス" },
          latest: null,
        },
      ],
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.totalArticles).toBe(64);
      expect(result.data.items[0]?.latest?.headline).toBe(
        "今週の AI ハイライト",
      );
      expect(result.data.items[0]?.latest?.summary).toBe("今週の総括リード");
      expect(result.data.items[0]?.latest?.inputArticleCount).toBe(64);
      expect(result.data.items[1]?.latest).toBeNull();
    }
  });

  it("accepts fields the schema does not know yet (backend 先行デプロイの安全条件)", () => {
    // 契約1 (keyArticles / watchPoints) を backend が先行追加しても、
    // zod 側が .strict() でない限り未知 field は無視されて parse が通る。
    // keyArticles[].article は契約1の実形 (ArticleBrief = 一覧カード契約) で再現する。
    const result = BriefingListResponseSchema.safeParse({
      currentWeekStart: "2026-04-27",
      totalArticles: 1,
      futureTopLevelField: true,
      items: [
        {
          category: { ...CATEGORY, futureCategoryField: "x" },
          latest: {
            weekStart: "2026-04-20",
            headline: "今週の AI ハイライト",
            summary: "今週の総括リード",
            inputArticleCount: 1,
            keyArticles: [
              {
                significance: "なぜ重要か",
                article: {
                  id: 1,
                  translatedTitle: "記事1",
                  keyPoints: ["要点A", "要点B"],
                  summaryPreview: null,
                  category: CATEGORY,
                  source: { name: "TechCrunch", attributionLabel: null },
                  publishedAt: "2026-04-19T09:00:00+09:00",
                },
              },
            ],
            watchPoints: ["今後どこを見るべきか"],
          },
          futureItemField: 1,
        },
      ],
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.items[0]?.latest?.headline).toBe(
        "今週の AI ハイライト",
      );
    }
  });

  it("rejects when currentWeekStart is not an ISO date", () => {
    const result = BriefingListResponseSchema.safeParse({
      currentWeekStart: "2026-04-27T00:00:00Z",
      totalArticles: 0,
      items: [],
    });
    expect(result.success).toBe(false);
  });

  it("rejects when latest is missing entirely (must be present as null or object)", () => {
    const result = BriefingListResponseSchema.safeParse({
      currentWeekStart: "2026-04-27",
      totalArticles: 0,
      items: [{ category: CATEGORY }],
    });
    expect(result.success).toBe(false);
  });
});

describe("BriefingResponseSchema", () => {
  it("narrows to detail when state='briefing' and required fields present", () => {
    const result = BriefingResponseSchema.safeParse({
      state: "briefing",
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
      keyArticles: [
        { significance: "なぜ重要か", article: ARTICLE_EMBED },
        {
          significance: "二件目の理由",
          article: {
            ...ARTICLE_EMBED,
            id: 2,
            translatedTitle: "記事2",
            source: { name: "Hacker News", attributionLabel: "HN 提供" },
            url: "https://y",
            publishedAt: null,
            keyPoints: [],
          },
        },
      ],
      watchPoints: ["今後どこを見るべきか"],
    });
    expect(result.success).toBe(true);
    if (result.success && result.data.state === "briefing") {
      expect(result.data.summary).toBe("今週の総括リード");
      expect(result.data.chapters[0]?.heading).toBe("資金とインフラ");
      expect(result.data.chapters[0]?.body).toBe(
        "今週は LLM 推論コスト削減と...",
      );
      expect(result.data.keyArticles.length).toBe(2);
      expect(result.data.keyArticles[0]?.significance).toBe("なぜ重要か");
      // keyArticle は記事 embed を自己完結で持つ (articles[] lookup は廃止)
      expect(result.data.keyArticles[0]?.article.id).toBe(1);
      expect(result.data.keyArticles[0]?.article.translatedTitle).toBe("記事1");
      expect(result.data.keyArticles[0]?.article.source.name).toBe(
        "TechCrunch",
      );
      expect(result.data.keyArticles[0]?.article.keyPoints).toEqual([
        "要点A",
        "要点B",
      ]);
      expect(result.data.keyArticles[1]?.article.publishedAt).toBeNull();
      expect(result.data.keyArticles[1]?.article.source.attributionLabel).toBe(
        "HN 提供",
      );
      expect(result.data.watchPoints).toEqual(["今後どこを見るべきか"]);
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
      state: "ready",
      category: CATEGORY,
    });
    expect(result.success).toBe(false);
  });

  it("rejects keyArticle whose article embed is missing", () => {
    const result = BriefingResponseSchema.safeParse({
      state: "briefing",
      weekStart: "2026-04-20",
      generatedAt: "2026-04-27T00:05:00+09:00",
      modelName: "deepseek-v4-pro",
      inputArticleCount: 1,
      category: CATEGORY,
      headline: "h",
      summary: "s",
      chapters: [{ heading: "h", body: "b" }],
      keyArticles: [{ significance: "なぜ重要か" }],
      watchPoints: [],
    });
    expect(result.success).toBe(false);
  });

  it("rejects detail missing required fields (e.g. summary/chapters)", () => {
    const result = BriefingResponseSchema.safeParse({
      state: "briefing",
      weekStart: "2026-04-20",
      generatedAt: "2026-04-27T00:05:00+09:00",
      modelName: "deepseek-v4-pro",
      inputArticleCount: 0,
      category: CATEGORY,
      headline: "h",
      keyArticles: [],
      watchPoints: [],
    });
    expect(result.success).toBe(false);
  });
});
