import { apiCall, typedServer } from "@/lib/api/typed-server-fetcher";
import { cacheTags } from "@/lib/cache/tags";
import type { PaginatedArticleResponse } from "@/types";

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
  return apiCall(
    typedServer.GET("/api/v1/me/watchlist", {
      params: { query: { page, perPage } },
      next: { tags: [cacheTags.watchlistMe] },
    }),
  );
}
