import { cacheLife } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import type { SemanticSearchQuery } from "@/types";
import { searchArticles as searchArticlesSdk } from "@/types/sdk.gen";
import type { PaginatedArticleResponse } from "@/types/types.gen";

/**
 * セマンティック検索 (response は user 非依存)。
 *
 * Backend `/api/v1/articles/search` は認可ガード不在 + docstring に user
 * 非依存と明記されているため、`publicClient` で全 user 共有 cache に
 * 乗せる。`cacheLife("seconds")` (stale 30s / revalidate 1s / expire 1m) は
 * 「ほぼ都度新鮮、同一 query を短時間に複数 user が叩いた場合のみ共有」の
 * プロファイル。引数 `query` (SemanticSearchQuery) が cache key になる。
 */
export async function searchArticles(
  query: SemanticSearchQuery,
): Promise<PaginatedArticleResponse> {
  "use cache";
  cacheLife("seconds");
  const { data } = await searchArticlesSdk({
    client: publicClient,
    throwOnError: true,
    query,
  });
  return data;
}
