/**
 * Type re-exports from generated OpenAPI types + manual supplementary types.
 *
 * Schema types are derived from backend/app/schemas/ via openapi-typescript.
 * Run `npm run generate-types` to regenerate types/generated.ts.
 */
import type { components, operations } from "./generated";

// ---------------------------------------------------------------------------
// Query parameter types — derived from `operations[...].parameters.query`.
// 手書きで `category?: string` 等を維持すると backend スキーマとの乖離 (例:
// `source` のような廃止/未実装キー) を frontend が抱え込む。OpenAPI を SSoT
// にして派生させ、backend が optional + nullable で表現するキーは frontend
// 側で `null` を剥がして optional のみに揃える。
// ---------------------------------------------------------------------------

type StripNull<T> = { [K in keyof T]: Exclude<T[K], null> };

/** Query parameters for GET /articles (article listing). */
export type ArticleQuery = StripNull<
  NonNullable<
    operations["list_articles_api_v1_articles_get"]["parameters"]["query"]
  >
>;

/** Query parameters for GET /articles/search (semantic search). */
export type SemanticSearchQuery = StripNull<
  NonNullable<
    operations["search_articles_api_v1_articles_search_get"]["parameters"]["query"]
  >
>;

// ---------------------------------------------------------------------------
// Re-exports from generated types
// ---------------------------------------------------------------------------

// Categories
export type CategoryBrief = Pick<
  components["schemas"]["CategoryDetail"],
  "slug" | "name"
>;
export type CategoryDetailResponse = components["schemas"]["CategoryDetail"];
export type CategoryDetailListResponse =
  components["schemas"]["CategoryDetailList"];

// Articles
export type ArticleBrief = components["schemas"]["ArticleBrief"];
export type ArticleDetail = components["schemas"]["ArticleDetail"];
export type PaginatedArticleResponse =
  components["schemas"]["PaginatedArticleResponse"];

// Watchlist
export type WatchlistIds = components["schemas"]["WatchlistIds"];

// ---------------------------------------------------------------------------
// Direct re-exports (no narrowing needed)
// ---------------------------------------------------------------------------

export type FetchRequest = components["schemas"]["FetchRequest"];
export type FetchResponse = components["schemas"]["FetchResponse"];

// News sources
export type NewsSourceEmbed = components["schemas"]["NewsSourceEmbed"];
export type NewsSourceDetail = components["schemas"]["NewsSourceDetail"];
export type NewsSourceDetailList =
  components["schemas"]["NewsSourceDetailList"];
export type NewsSourceCreate = components["schemas"]["NewsSourceCreate"];

// Weekly trends
// `WeeklyTrendsResponse` は backend で Annotated[Union, Field(discriminator)] alias
// として定義されており、独立した component schema を持たない (response inline)。
// `state` discriminator で frontend が narrowing できるよう、生成済の 2 状態
// schema をここで union として再構築する。
export type ReadyWeeklyTrends = components["schemas"]["ReadyWeeklyTrends"];
export type EmptyWeeklyTrends = components["schemas"]["EmptyWeeklyTrends"];
export type WeeklyTrendsResponse = ReadyWeeklyTrends | EmptyWeeklyTrends;
export type WeeklyCategoryTrends = components["schemas"]["_CategoryTrendsOut"];
export type WeeklyEntityTrend = components["schemas"]["_EntityTrendOut"];
export type WeeklyTopicTrend = components["schemas"]["_TopicTrendOut"];
export type WeeklyNewEntity = components["schemas"]["_NewEntityOut"];
