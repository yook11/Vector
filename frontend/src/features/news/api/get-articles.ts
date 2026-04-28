import { serverFetch } from "@/lib/api/server-fetcher";
import type { ArticleQuery, PaginatedArticleResponse } from "@/types";

/** Fetch paginated article list with optional filters. */
export async function getArticles(
  query?: ArticleQuery,
): Promise<PaginatedArticleResponse> {
  const params = new URLSearchParams();
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return serverFetch<PaginatedArticleResponse>(
    `/articles${qs ? `?${qs}` : ""}`,
    { next: { revalidate: 300, tags: ["articles"] } },
  );
}
