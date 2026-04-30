import { cacheLife } from "next/cache";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { PaginatedArticleResponse, SemanticSearchQuery } from "@/types";

/**
 * セマンティック検索 (response は user 非依存)。
 *
 * Backend `/api/v1/articles/search` は認可ガード不在 + docstring に user
 * 非依存と明記されているため、`publicServerFetch` で全 user 共有 cache に
 * 乗せる。`cacheLife("seconds")` (stale 30s / revalidate 1s / expire 1m) は
 * 「ほぼ都度新鮮、同一 query を短時間に複数 user が叩いた場合のみ共有」の
 * プロファイル。引数 `query` (SemanticSearchQuery) が cache key になる。
 */
export async function searchArticles(
  query: SemanticSearchQuery,
): Promise<PaginatedArticleResponse> {
  "use cache";
  cacheLife("seconds");
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  return publicServerFetch<PaginatedArticleResponse>(
    `/articles/search?${params.toString()}`,
  );
}
