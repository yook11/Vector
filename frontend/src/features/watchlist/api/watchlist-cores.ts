/**
 * Server Action 内部の HTTP 構築ロジック (pure 関数群)。
 *
 * 副作用 (guard / updateTag) は wrapper 側の Server Action に残す。
 * 詳細は features/sources/api/source-cores.ts のコメント参照。
 *
 * PR-Y3 で旧 `serverEmpty` から `typedServer` (openapi-fetch ベース) に移行
 * した exemplar。path / method / body の型は generated.ts の paths から自動
 * 導出される。
 */

import { apiVoid, type typedServer } from "@/lib/api/typed-server-fetcher";

export async function addToWatchlistCore(
  articleId: number,
  fetcher: typeof typedServer,
): Promise<void> {
  await apiVoid(
    fetcher.POST("/api/v1/me/watchlist", {
      body: { articleId },
    }),
  );
}

export async function removeFromWatchlistCore(
  articleId: number,
  fetcher: typeof typedServer,
): Promise<void> {
  await apiVoid(
    fetcher.DELETE("/api/v1/me/watchlist/{article_id}", {
      params: { path: { article_id: articleId } },
    }),
  );
}
