import { apiCall, typedServer } from "@/lib/api/typed-server-fetcher";
import { getCurrentSession } from "@/lib/auth/guards";
import { cacheTags } from "@/lib/cache/tags";

/**
 * 認証済 user の watched article ID 集合を取得する。
 *
 * Pattern B: ウォッチ状態は記事リソースに含めず独立リソース化することで、
 * `/articles` 系 response が user 非依存になり `'use cache'` で全 user
 * 共有できる。本関数のみ per-user で fetch し、render 時に Set lookup で
 * merge する。未ログインは空 Set を返す。
 *
 * cache 戦略: `next.tags: [cacheTags.watchlistMe]` で server data cache に
 * 乗せ、Server Action 後の `updateTag(cacheTags.watchlistMe)` で immediate
 * 無効化する。
 * per-user 分離は `typedServer` 経由で付与される `Authorization` header
 * (HS256 JWT) が Next.js data cache の cache key に含まれることで担保。
 *
 * PR-Y3 で旧 `serverFetch<WatchlistIds>("/me/watchlist/ids", ...)` から
 * `typedServer.GET("/api/v1/me/watchlist/ids", ...)` に移行した exemplar。
 * response 型 (`{ ids: number[] }`) は generated.ts の paths から自動導出。
 */
export async function getWatchlistIds(): Promise<Set<number>> {
  const session = await getCurrentSession();
  if (!session) return new Set();
  const data = await apiCall(
    typedServer.GET("/api/v1/me/watchlist/ids", {
      next: { tags: [cacheTags.watchlistMe] },
    }),
  );
  return new Set(data.ids);
}
