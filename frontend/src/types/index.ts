/**
 * Type re-exports for narrowing / alias / discriminated union 集約点。
 *
 * Schema types は `backend/app/schemas/` の Pydantic から `@hey-api/openapi-ts`
 * 経由で `types.gen.ts` に自動生成される。`npm run generate-types` で再生成。
 *
 * 本ファイルの責務:
 * - StripNull narrowing: backend が optional + nullable で表現するキーを frontend
 *   側で `null` を剥がして optional のみに揃える
 * - Pick narrowing: 大型 schema から component で必要な field のみ抽出
 * - alias rename: Pydantic 内部 schema 名 (`StoryOut` 等) を frontend public 名へ
 * - discriminated union 再構築: `Annotated[Union, Field(discriminator)]` alias は
 *   openapi.json で oneOf に展開されるが Python alias 名は component schema 化
 *   されないため frontend 側で組み直す (`BriefingResponse` / `WeeklyTrendsResponse`)
 *
 * 単純 re-export (ArticleBrief / ArticleDetail / NewsSourceDetail 等) は本ファイル
 * から撤廃済 (PR-H3)。利用側は `@/types/types.gen` から直接 import する。
 */
import type {
  EmptyBriefing as _EmptyBriefing,
  EmptyWeeklyTrends as _EmptyWeeklyTrends,
  ReadyBriefing as _ReadyBriefing,
  ReadyWeeklyTrends as _ReadyWeeklyTrends,
  ArticleSummaryOut,
  BriefingListLatest,
  CategoryDetail,
  CategoryOut,
  CategoryTrendsOut,
  EntityTrendOut,
  ListArticlesData,
  NewEntityOut,
  SearchArticlesData,
  StoryOut,
  TopicTrendOut,
} from "@/types/types.gen";

// ---------------------------------------------------------------------------
// StripNull narrowing
// ---------------------------------------------------------------------------

type StripNull<T> = { [K in keyof T]: Exclude<T[K], null> };

/** Query parameters for GET /articles (article listing). */
export type ArticleQuery = StripNull<NonNullable<ListArticlesData["query"]>>;

/** Query parameters for GET /articles/search (semantic search). */
export type SemanticSearchQuery = StripNull<
  NonNullable<SearchArticlesData["query"]>
>;

// ---------------------------------------------------------------------------
// Pick narrowing
// ---------------------------------------------------------------------------

export type CategoryBrief = Pick<CategoryDetail, "slug" | "name">;

// ---------------------------------------------------------------------------
// Alias rename — Pydantic 内部 schema 名 → frontend public 名
// ---------------------------------------------------------------------------

export type BriefingStory = StoryOut;
export type BriefingArticleSummary = ArticleSummaryOut;
export type BriefingCategory = CategoryOut;
export type { BriefingListLatest };
export type WeeklyCategoryTrends = CategoryTrendsOut;
export type WeeklyEntityTrend = EntityTrendOut;
export type WeeklyTopicTrend = TopicTrendOut;
export type WeeklyNewEntity = NewEntityOut;

// ---------------------------------------------------------------------------
// Discriminated union 再構築
//
// types.gen.ts では `state?: 'ready' | 'empty'` (optional) として生成される。
// 背景: backend Pydantic で `state: Literal["ready"] = "ready"` のように default
// 値があると FastAPI が OpenAPI 上 `required: false` で出すため、hey-api は
// optional として型生成する。openapi-typescript は discriminator 付きのケース
// を required に補正していたが hey-api はしない。
//
// optional のままでは `if (data.state === "empty")` での narrowing が効かない
// (`data.state` が undefined の経路が残る) ため、frontend 側で intersection
// で required に補強し、利用側の narrowing シンタックスを変えずに済ませる。
// ---------------------------------------------------------------------------

export type ReadyBriefing = _ReadyBriefing & { state: "ready" };
export type EmptyBriefing = _EmptyBriefing & { state: "empty" };
export type BriefingResponse = ReadyBriefing | EmptyBriefing;

export type ReadyWeeklyTrends = _ReadyWeeklyTrends & { state: "ready" };
export type EmptyWeeklyTrends = _EmptyWeeklyTrends & { state: "empty" };
export type WeeklyTrendsResponse = ReadyWeeklyTrends | EmptyWeeklyTrends;
