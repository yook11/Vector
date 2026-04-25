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

/** Query parameters for GET /articles (article listing). */
export interface ArticleQuery {
  category?: string;
  impactLevel?: ImpactLevel;
  source?: string;
  sortOrder?: "asc" | "desc";
  page?: number;
  perPage?: number;
}

/** Query parameters for GET /articles/search (semantic search). */
export interface SemanticSearchQuery {
  q: string;
  sortBy?: "date" | "relevance";
  category?: string;
  impactLevel?: ImpactLevel;
  source?: string;
  sortOrder?: "asc" | "desc";
  page?: number;
  perPage?: number;
}

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

// ---------------------------------------------------------------------------
// Narrowed types — where generated types need refinement
// ---------------------------------------------------------------------------

/** Article brief (list card) — narrows impactLevel to ImpactLevel union. */
export type ArticleBrief = Omit<
  components["schemas"]["ArticleBrief"],
  "impactLevel"
> & {
  impactLevel: ImpactLevel;
};

/** Article detail (single article) — narrows impactLevel to ImpactLevel union. */
export type ArticleDetail = Omit<
  components["schemas"]["ArticleDetail"],
  "impactLevel"
> & {
  impactLevel: ImpactLevel;
};

/** Narrow items to use our narrowed ArticleBrief. */
export type PaginatedArticleResponse = Omit<
  components["schemas"]["PaginatedArticleResponse"],
  "items"
> & {
  items: ArticleBrief[];
};

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
