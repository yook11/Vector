import "@/lib/api/hey-api-interceptors";
import { getCurrentSession } from "@/lib/auth/guards";
import { cacheTags } from "@/lib/cache/tags";
import { listWatchlistIds } from "@/types/sdk.gen";

/**
 * 渡した記事のうち、認証済み user がウォッチ中の記事 ID 集合を取得する。
 *
 * ウォッチ状態は記事リソースに含めず独立リソースとして取得する。
 * `/articles` 系 response は user 非依存のまま cache し、render 時に Set lookup で
 * merge する。未ログイン、または記事が無いときは問い合わせずに空 Set を返す。
 *
 * cache 戦略: `next.tags: [cacheTags.watchlistMe]` で server data cache に
 * 乗せ、Server Action 後の `updateTag(cacheTags.watchlistMe)` で immediate
 * 無効化する。
 * per-user 分離は singleton `client` (auth interceptor) で付与される
 * `Authorization` header (HS256 JWT) が Next.js data cache の cache key に
 * 含まれることで担保。
 */
export async function getWatchlistIds(
  articleIds: readonly number[],
): Promise<Set<number>> {
  if (articleIds.length === 0) return new Set();
  const session = await getCurrentSession();
  if (!session) return new Set();
  const { data } = await listWatchlistIds({
    query: { articleIds: [...articleIds] },
    throwOnError: true,
    next: { tags: [cacheTags.watchlistMe] },
  });
  return new Set(data.ids);
}
