import { cacheLife, cacheTag } from "next/cache";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { ArticleQuery, PaginatedArticleResponse } from "@/types";

/**
 * Fetch paginated article list with optional filters.
 *
 * Pattern B: response は user 非依存 (ウォッチ状態は `getWatchlistIds` で
 * 別取得し render 時に Set lookup で merge)。`publicServerFetch` + `'use cache'`
 * で全 user 共有のキャッシュに乗せる。
 */
export async function getArticles(
  query?: ArticleQuery,
): Promise<PaginatedArticleResponse> {
  "use cache";
  cacheLife("minutes");
  cacheTag("articles");
  const params = new URLSearchParams();
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return publicServerFetch<PaginatedArticleResponse>(
    `/articles${qs ? `?${qs}` : ""}`,
  );
}
