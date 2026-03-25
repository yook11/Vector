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

// Categories (unified — replaces both KeywordCategory and InvestmentCategory)
export type CategoryBrief = components["schemas"]["CategoryBrief"];
export type KeywordInCategory = components["schemas"]["KeywordInCategory"];
export type CategoryDetailResponse =
  components["schemas"]["CategoryDetailResponse"];
export type CategoryDetailListResponse =
  components["schemas"]["CategoryDetailListResponse"];

// Keywords
export type KeywordBrief = components["schemas"]["KeywordBrief"];
export type KeywordResponse = components["schemas"]["KeywordResponse"];
export type KeywordListResponse = components["schemas"]["KeywordListResponse"];
export type KeywordCreate = components["schemas"]["KeywordCreate"];
export type KeywordUpdate = components["schemas"]["KeywordUpdate"];

// ---------------------------------------------------------------------------
// Narrowed types — where generated types need refinement
// ---------------------------------------------------------------------------

/** Analysis response — narrows impactLevel from string to ImpactLevel union. */
export type AnalysisResponse = Omit<
  components["schemas"]["AnalysisResponse"],
  "impactLevel"
> & {
  impactLevel: ImpactLevel;
};

/** Narrow nested analysis to use our narrowed AnalysisResponse. */
export type NewsResponse = Omit<
  components["schemas"]["NewsResponse"],
  "analysis"
> & {
  analysis?: AnalysisResponse | null;
};

/** Narrow items to use our narrowed NewsResponse. */
export type PaginatedNewsResponse = Omit<
  components["schemas"]["PaginatedNewsResponse"],
  "items"
> & {
  items: NewsResponse[];
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
export type NewsSourceResponse = components["schemas"]["NewsSourceResponse"];
export type NewsSourceListResponse =
  components["schemas"]["NewsSourceListResponse"];
export type NewsSourceCreate = components["schemas"]["NewsSourceCreate"];
export type NewsSourceUpdate = components["schemas"]["NewsSourceUpdate"];
