import { cacheLife } from "next/cache";
import { apiCall, typedPublic } from "@/lib/api/typed-server-fetcher";
import type { ArticleQuery } from "@/types";
import type { PaginatedArticleResponse } from "@/types/types.gen";

/**
 * 記事一覧取得 (response は user 非依存)。
 *
 * Backend response は user 非依存 (ウォッチ状態は `getWatchlistIds` で別途
 * 取得し、render 時に Set lookup で merge)。`typedPublic` + `'use cache'`
 * で全 user 共有 cache に乗せる。
 *
 * `cacheLife("minutes")` は stale 5min / revalidate 1min / expire 1h の公式
 * プロファイル。記事 ingestion 周期 (~30 分) に対し revalidate 1 分は十分
 * 新鮮。expire 1h は long tail traffic 用の上限。
 *
 * cache key は引数 `query` のシリアライズで決まる。`ArticleQuery` の shape
 * は callsite の `parseArticleQuery` で zod 検証通過後に常に同 shape で
 * 確定するため、`Object.entries` の挿入順序による cache pollution は
 * structural に防がれている。`query ?? {}` で undefined を空 object に
 * 正規化し、cache key 安定化を担保する。
 */
export async function getArticles(
  query?: ArticleQuery,
): Promise<PaginatedArticleResponse> {
  "use cache";
  cacheLife("minutes");
  return apiCall(
    typedPublic.GET("/api/v1/articles", { params: { query: query ?? {} } }),
  );
}
