import { serverFetch } from "@/lib/api/server-fetcher";
import { getCurrentSession } from "@/lib/auth/guards";
import type { WatchlistIds } from "@/types";

/**
 * 認証済 user の watched article ID 集合を取得する。
 *
 * Pattern B: ウォッチ状態は記事リソースに含めず独立リソース化することで、
 * `/articles` 系 response が user 非依存になり `'use cache'` で全 user
 * 共有できる。本関数のみ per-user で fetch し、render 時に Set lookup で
 * merge する。未ログインは空 Set を返す。
 */
export async function getWatchlistIds(): Promise<Set<number>> {
  const session = await getCurrentSession();
  if (!session) return new Set();
  const res = await serverFetch<WatchlistIds>("/me/watchlist/ids", {
    cache: "no-store",
    next: { tags: ["watchlist:me"] },
  });
  return new Set(res.ids);
}
