/**
 * Server Action 内部の HTTP 構築ロジック (pure 関数群)。
 *
 * 副作用 (guard / updateTag) は wrapper 側の Server Action に残す。
 * 詳細は features/sources/api/source-cores.ts のコメント参照。
 */

import type { serverEmpty } from "@/lib/api/server-fetcher";

export async function addToWatchlistCore(
  articleId: number,
  fetcher: typeof serverEmpty,
): Promise<void> {
  await fetcher("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ articleId }),
  });
}

export async function removeFromWatchlistCore(
  articleId: number,
  fetcher: typeof serverEmpty,
): Promise<void> {
  await fetcher(`/me/watchlist/${articleId}`, {
    method: "DELETE",
  });
}
