/**
 * Server Action 内部の HTTP 構築ロジック (pure 関数群)。
 *
 * 副作用 (guard / updateTag) は wrapper 側の Server Action に残す。
 * 詳細は features/sources/api/source-cores.ts のコメント参照。
 *
 * fetcher は hey-api 生成の SDK 関数 (`addToWatchlist` / `removeFromWatchlist`)
 * と同じ signature を持つ。auth header 注入は side-effect import した
 * `hey-api-interceptors` の singleton client 経由で実施される。
 */

import "@/lib/api/hey-api-interceptors";
import type {
  addToWatchlist as addToWatchlistSdk,
  removeFromWatchlist as removeFromWatchlistSdk,
} from "@/types/sdk.gen";

export async function addToWatchlistCore(
  articleId: number,
  fetcher: typeof addToWatchlistSdk,
): Promise<void> {
  await fetcher({
    throwOnError: true,
    body: { articleId },
  });
}

export async function removeFromWatchlistCore(
  articleId: number,
  fetcher: typeof removeFromWatchlistSdk,
): Promise<void> {
  await fetcher({
    throwOnError: true,
    path: { article_id: articleId },
  });
}
