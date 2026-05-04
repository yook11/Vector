import "@/lib/api/hey-api-interceptors";
import { cacheTags } from "@/lib/cache/tags";
import { listArticlesInWatchlist } from "@/types/sdk.gen";
import type { PaginatedArticleResponse } from "@/types/types.gen";

/**
 * ユーザの watchlist 一覧を取得する。
 *
 * cache 戦略: `getWatchlistIds` と同じ `cacheTags.watchlistMe` に乗せ、
 * Server Action 後の `updateTag(cacheTags.watchlistMe)` で両者を同時無効化
 * する。per-user 分離は `Authorization` header (HS256 JWT) が data cache の
 * cache key に含まれることで担保 (詳細は `get-watchlist-ids.ts` の docstring
 * を参照)。
 */
export async function getWatchlist(
  page = 1,
  perPage = 20,
): Promise<PaginatedArticleResponse> {
  const { data } = await listArticlesInWatchlist({
    throwOnError: true,
    query: { page, perPage },
    next: { tags: [cacheTags.watchlistMe] },
  });
  return data;
}
