/**
 * Briefing API レスポンスの zod schema。
 *
 * backend (Pydantic) を SSoT とし、frontend では受信時の構造検証 + TS narrowing
 * 軸として保持する。生成型 (`@/types`) は静的形状、zod は runtime 検証。
 *
 * 一覧 (`BriefingListResponse`) は nullable nested (`latest: T | None`) で
 * 「ある/ない」を表現。詳細 (`BriefingResponse`) は state field discriminator
 * で `briefing` / `empty` を分岐 (差分フィールド多数のため discriminated union 採用)。
 */

import { z } from "zod";

const CategorySchema = z.object({
  slug: z.string().min(1),
  name: z.string().min(1),
});

const BriefingSummarySchema = z.object({
  weekStart: z.iso.date(),
  headline: z.string(),
  summary: z.string(),
  inputArticleCount: z.number(),
});

const BriefingListItemSchema = z.object({
  category: CategorySchema,
  latest: BriefingSummarySchema.nullable(),
});

export const BriefingListResponseSchema = z.object({
  currentWeekStart: z.iso.date(),
  totalArticles: z.number(),
  items: z.array(BriefingListItemSchema),
});

const NewsSourceEmbedSchema = z.object({
  name: z.string(),
  attributionLabel: z.string().nullable(),
});

const BriefingArticleEmbedSchema = z.object({
  // /news/{id} 記事詳細の公開 id (ArticleBrief.id と同じ id 空間)
  id: z.number(),
  translatedTitle: z.string(),
  source: NewsSourceEmbedSchema,
  url: z.string(),
  // 元記事の公開日時 (Article.published_at)。未取得記事は null。
  publishedAt: z.iso.datetime({ offset: true }).nullable(),
  keyPoints: z.array(z.string()),
});

const ChapterSchema = z.object({
  heading: z.string(),
  body: z.string(),
});

const KeyArticleSchema = z.object({
  significance: z.string(),
  article: BriefingArticleEmbedSchema,
});

const BriefingDetailSchema = z.object({
  state: z.literal("briefing"),
  weekStart: z.iso.date(),
  generatedAt: z.iso.datetime({ offset: true }),
  modelName: z.string(),
  inputArticleCount: z.number(),
  category: CategorySchema,
  headline: z.string(),
  summary: z.string(),
  chapters: z.array(ChapterSchema),
  keyArticles: z.array(KeyArticleSchema),
  watchPoints: z.array(z.string()),
});

const EmptyBriefingSchema = z.object({
  state: z.literal("empty"),
  category: CategorySchema,
});

export const BriefingResponseSchema = z.discriminatedUnion("state", [
  BriefingDetailSchema,
  EmptyBriefingSchema,
]);

export type BriefingListResponseParsed = z.infer<
  typeof BriefingListResponseSchema
>;
export type BriefingResponseParsed = z.infer<typeof BriefingResponseSchema>;
export type BriefingKeyArticleParsed = z.infer<typeof KeyArticleSchema>;
export type BriefingArticleEmbedParsed = z.infer<
  typeof BriefingArticleEmbedSchema
>;
