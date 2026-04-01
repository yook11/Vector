/**
 * Type re-exports from generated OpenAPI types + manual supplementary types.
 *
 * Schema types are derived from backend/app/schemas/ via openapi-typescript.
 * Run `npm run generate-types` to regenerate types/generated.ts.
 */
import type { components } from "./generated";

// ---------------------------------------------------------------------------
// Manual types — not directly derivable from OpenAPI schema
// ---------------------------------------------------------------------------

/** Impact levels for news analysis. */
export type ImpactLevel = "low" | "medium" | "high" | "critical";

/** Query parameters for GET /news (client-side helper). */
export interface NewsQuery {
  q?: string;
  keywordId?: number;
  kwCategoryId?: number;
  impactLevel?: ImpactLevel;
  sourceId?: number;
  sortBy?: "publishedAt" | "impactLevel";
  sortOrder?: "asc" | "desc";
  page?: number;
  perPage?: number;
}

// ---------------------------------------------------------------------------
// Re-exports from generated types
// ---------------------------------------------------------------------------

// Categories
export type CategoryBrief = components["schemas"]["CategoryEmbed"];
export type CategoryDetailResponse = components["schemas"]["CategoryDetail"];
export type CategoryDetailListResponse =
  components["schemas"]["CategoryDetailList"];

// Keywords
export type KeywordResponse = components["schemas"]["KeywordDetail"];
export type KeywordListResponse = components["schemas"]["KeywordDetailList"];
export type KeywordCreate = components["schemas"]["KeywordCreate"];
export type KeywordUpdate = components["schemas"]["KeywordUpdate"];

// ---------------------------------------------------------------------------
// Narrowed types — where generated types need refinement
// ---------------------------------------------------------------------------

/** News brief (list card) — narrows impactLevel to ImpactLevel union. */
export type NewsBrief = Omit<
  components["schemas"]["NewsBrief"],
  "impactLevel"
> & {
  impactLevel: ImpactLevel;
};

/** News detail (single article) — narrows impactLevel to ImpactLevel union. */
export type NewsDetail = Omit<
  components["schemas"]["NewsDetail"],
  "impactLevel"
> & {
  impactLevel: ImpactLevel;
};

/** Narrow items to use our narrowed NewsBrief. */
export type PaginatedNewsResponse = Omit<
  components["schemas"]["PaginatedNewsResponse"],
  "items"
> & {
  items: NewsBrief[];
};

// ---------------------------------------------------------------------------
// Direct re-exports (no narrowing needed)
// ---------------------------------------------------------------------------

export type NewsFetchRequest = components["schemas"]["NewsFetchRequest"];
export type NewsFetchResponse = components["schemas"]["NewsFetchResponse"];
export type WatchlistResponse = components["schemas"]["WatchlistResponse"];
export type WatchlistListResponse =
  components["schemas"]["WatchlistListResponse"];

// News sources
export type NewsSourceEmbed = components["schemas"]["NewsSourceEmbed"];
export type NewsSourceDetail = components["schemas"]["NewsSourceDetail"];
export type NewsSourceDetailList =
  components["schemas"]["NewsSourceDetailList"];
export type NewsSourceCreate = components["schemas"]["NewsSourceCreate"];
