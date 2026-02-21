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

/** Sentiment categories for news analysis. */
export type Sentiment = "positive" | "negative" | "neutral";

/** Query parameters for GET /news (client-side helper). */
export interface NewsQuery {
  keywordId?: number;
  myKeywords?: boolean;
  sentiment?: Sentiment;
  minImpact?: number;
  sortBy?: "publishedAt" | "impactScore";
  sortOrder?: "asc" | "desc";
  page?: number;
  perPage?: number;
}

// ---------------------------------------------------------------------------
// Re-exports from generated types — with narrowing where needed
// ---------------------------------------------------------------------------

/** Narrow sentiment from string to Sentiment literal union. */
export type AnalysisResponse = Omit<
  components["schemas"]["AnalysisResponse"],
  "sentiment"
> & {
  sentiment: Sentiment;
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

// Direct re-exports (no narrowing needed)
export type KeywordBrief = components["schemas"]["KeywordBrief"];
export type KeywordResponse = components["schemas"]["KeywordResponse"];
export type KeywordListResponse = components["schemas"]["KeywordListResponse"];
export type KeywordCreate = components["schemas"]["KeywordCreate"];
export type KeywordUpdate = components["schemas"]["KeywordUpdate"];
export type NewsFetchRequest = components["schemas"]["NewsFetchRequest"];
export type NewsFetchResponse = components["schemas"]["NewsFetchResponse"];
export type LoginRequest = components["schemas"]["LoginRequest"];
export type RegisterRequest = components["schemas"]["RegisterRequest"];
export type TokenResponse = components["schemas"]["TokenResponse"];
export type UserResponse = components["schemas"]["UserResponse"];
export type SubscriptionResponse = components["schemas"]["SubscriptionResponse"];
export type SubscriptionListResponse =
  components["schemas"]["SubscriptionListResponse"];
export type WatchlistResponse = components["schemas"]["WatchlistResponse"];
export type WatchlistListResponse =
  components["schemas"]["WatchlistListResponse"];
