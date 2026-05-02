/**
 * Briefing API レスポンスの zod schema。
 *
 * backend (Pydantic) を SSoT とし、frontend では受信時の構造検証 + TS narrowing
 * 軸として保持する。生成型 (`@/types`) は静的形状、zod は runtime 検証。
 *
 * 一覧 (`BriefingListResponse`) は nullable nested (`latest: T | None`) で
 * 「ある/ない」を表現。詳細 (`BriefingResponse`) は state field discriminator
 * で `ready` / `empty` を分岐 (差分フィールド多数のため discriminated union 採用)。
 */

import { z } from "zod";

const CategorySchema = z.object({
  id: z.number(),
  slug: z.string().min(1),
  name: z.string().min(1),
});

const BriefingListLatestSchema = z.object({
  weekStart: z.iso.date(),
  headlineExcerpt: z.string(),
});

const BriefingListItemSchema = z.object({
  category: CategorySchema,
  latest: BriefingListLatestSchema.nullable(),
});

export const BriefingListResponseSchema = z.object({
  currentWeekStart: z.iso.date(),
  items: z.array(BriefingListItemSchema),
});

const BriefingArticleSummarySchema = z.object({
  id: z.number(),
  titleJa: z.string(),
  sourceName: z.string(),
  url: z.string(),
});

const BriefingStorySchema = z.object({
  title: z.string(),
  analysis: z.string(),
  articleIds: z.array(z.number()),
});

const ReadyBriefingSchema = z.object({
  state: z.literal("ready"),
  weekStart: z.iso.date(),
  generatedAt: z.iso.datetime({ offset: true }),
  modelName: z.string(),
  inputArticleCount: z.number(),
  category: CategorySchema,
  headline: z.string(),
  stories: z.array(BriefingStorySchema),
  articles: z.array(BriefingArticleSummarySchema),
});

const EmptyBriefingSchema = z.object({
  state: z.literal("empty"),
  category: CategorySchema,
});

export const BriefingResponseSchema = z.discriminatedUnion("state", [
  ReadyBriefingSchema,
  EmptyBriefingSchema,
]);

export type BriefingListResponseParsed = z.infer<
  typeof BriefingListResponseSchema
>;
export type BriefingResponseParsed = z.infer<typeof BriefingResponseSchema>;
