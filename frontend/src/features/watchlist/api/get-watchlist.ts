import { serverFetch } from "@/lib/api/server-fetcher";
import type { PaginatedArticleResponse } from "@/types";

/**
 * ユーザの watchlist 一覧を取得する。
 *
 * cache 戦略: `getWatchlistIds` と同じ `watchlist:me` tag に乗せ、Server Action
 * 後の `updateTag("watchlist:me")` で両者を同時無効化する。per-user 分離は
 * `Authorization` header (HS256 JWT) が data cache の cache key に含まれる
 * ことで担保 (詳細は `get-watchlist-ids.ts` の docstring を参照)。
 */
export async function getWatchlist(
  page = 1,
  perPage = 20,
): Promise<PaginatedArticleResponse> {
  return serverFetch<PaginatedArticleResponse>(
    `/me/watchlist?page=${page}&perPage=${perPage}`,
    { next: { tags: ["watchlist:me"] } },
  );
}
