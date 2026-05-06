import type { SemanticSearchQuery } from "@/types";
import { searchArticles as searchArticlesSdk } from "@/types/sdk.gen";
import type { PaginatedArticleResponse } from "@/types/types.gen";

/**
 * セマンティック検索 (per-user quota 適用済 / auth 必須)。
 *
 * red-team C1 対策で backend に認証 + per-user 日次 quota を導入したため、
 * response は user 非依存だが共有 cache は不可能 (auth interceptor が cookies /
 * headers を読むため `"use cache"` 内禁止操作)。auth-required endpoint と同様に
 * default `client` (interceptor 付き singleton) を使う。
 */
export async function searchArticles(
  query: SemanticSearchQuery,
): Promise<PaginatedArticleResponse> {
  const { data } = await searchArticlesSdk({
    throwOnError: true,
    query,
  });
  return data;
}
