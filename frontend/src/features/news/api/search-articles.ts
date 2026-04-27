import { serverFetch } from "@/lib/api/server-fetcher";
import type { PaginatedArticleResponse, SemanticSearchQuery } from "@/types";

/** Search articles by semantic similarity. */
export async function searchArticles(
  query: SemanticSearchQuery,
): Promise<PaginatedArticleResponse> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  return serverFetch<PaginatedArticleResponse>(
    `/articles/search?${params.toString()}`,
    { cache: "no-store" },
  );
}
