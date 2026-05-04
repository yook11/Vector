import "@/lib/api/hey-api-interceptors";
import { getCurrentSession } from "@/lib/auth/guards";
import { cacheTags } from "@/lib/cache/tags";
import { listWatchlistIds } from "@/types/sdk.gen";

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
 * per-user 分離は singleton `client` (auth interceptor) で付与される
 * `Authorization` header (HS256 JWT) が Next.js data cache の cache key に
 * 含まれることで担保。
 */
export async function getWatchlistIds(): Promise<Set<number>> {
  const session = await getCurrentSession();
  if (!session) return new Set();
  const { data } = await listWatchlistIds({
    throwOnError: true,
    next: { tags: [cacheTags.watchlistMe] },
  });
  return new Set(data.ids);
}
