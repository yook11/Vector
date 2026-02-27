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
  category?: string;
  sortBy?: "publishedAt" | "impactScore";
  sortOrder?: "asc" | "desc";
  page?: number;
  perPage?: number;
  locale?: string;
}

/** Investment category (brief, embedded in AnalysisResponse). */
export interface CategoryBrief {
  slug: string;
  name: string;
}

/** Investment category (full, from GET /api/v1/categories). */
export interface CategoryResponse {
  id: number;
  slug: string;
  name: string;
  description?: string | null;
}

/** Response wrapper for GET /api/v1/categories. */
export interface CategoryListResponse {
  items: CategoryResponse[];
}

/** Keyword category (brief, embedded in KeywordResponse / SubscriptionResponse). */
export interface KeywordCategoryBrief {
  slug: string;
  name: string;
}

/** Keyword category response from GET /api/v1/keyword-categories. */
export interface KeywordCategoryResponse {
  id: number;
  slug: string;
  name: string;
}

/** Response wrapper for GET /api/v1/keyword-categories. */
export interface KeywordCategoryListResponse {
  items: KeywordCategoryResponse[];
}

// ---------------------------------------------------------------------------
// Re-exports from generated types — with narrowing where needed
// ---------------------------------------------------------------------------

/** Analysis response — overridden until generated.ts is regenerated. */
export interface AnalysisResponse {
  title: string;
  summary: string;
  sentiment: Sentiment;
  impactScore: number;
  reasoning?: string | null;
  aiProvider: string;
  analyzedAt: string;
  investmentCategories?: CategoryBrief[];
}

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

// Keyword types — overridden until generated.ts is regenerated
// (generated.ts still has stale category/isActive fields)

export interface KeywordBrief {
  id: number;
  keyword: string;
  categories: KeywordCategoryBrief[];
}

export interface KeywordResponse {
  id: number;
  keyword: string;
  categories: KeywordCategoryBrief[];
  articleCount: number;
  createdAt: string;
}

export interface KeywordListResponse {
  items: KeywordResponse[];
}

export interface KeywordCreate {
  keyword: string;
  categoryIds?: number[];
}

export interface KeywordUpdate {
  categoryIds?: number[] | null;
}
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
