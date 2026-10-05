import { cacheLife, cacheTag } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import { cacheTags } from "@/lib/cache/tags";
import type { ArticleQuery } from "@/types";
import { listArticles } from "@/types/sdk.gen";
import type { AnalyzedArticlePreviewList } from "@/types/types.gen";

/**
 * 記事一覧をカーソルの位置から1回分取得する (response は user 非依存)。
 *
 * Backend response は user 非依存 (ウォッチ状態は `getWatchlistIds` で別途
 * 取得し、render 時に Set lookup で merge)。`publicClient` は session を読まず
 * BFF 経由証明だけを付けるので `"use cache"` 内で `cookies()/headers()` を
 * 踏まずに済む。
 *
 * `cacheLife("minutes")` は stale 5min / revalidate 1min / expire 1h の公式
 * プロファイル。記事 ingestion 周期 (~30 分) に対し revalidate 1 分は十分
 * 新鮮。expire 1h は long tail traffic 用の上限。
 *
 * cache key は引数 `query` (category とカーソル) と表示開始時の
 * `articleListRevision` で決まる。同じ条件が同じ key になるよう、呼び出し側は
 * 値のあるキーだけを category → cursor の順で持たせる。
 */
export async function getArticles(
  query: ArticleQuery | undefined,
  articleListRevision: string,
): Promise<AnalyzedArticlePreviewList> {
  "use cache";
  cacheLife("minutes");
  cacheTag(cacheTags.articlesList);
  void articleListRevision;
  const { data } = await listArticles({
    client: publicClient,
    throwOnError: true,
    query: query ?? {},
  });
  return data;
}
