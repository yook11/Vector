import { cacheLife } from "next/cache";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { ArticleQuery, PaginatedArticleResponse } from "@/types";

/**
 * 記事一覧取得 (response は user 非依存)。
 *
 * Backend response は user 非依存 (ウォッチ状態は `getWatchlistIds` で別途
 * 取得し、render 時に Set lookup で merge)。`publicServerFetch` + `'use cache'`
 * で全 user 共有 cache に乗せる。
 *
 * `cacheLife("minutes")` は stale 5min / revalidate 1min / expire 1h の公式
 * プロファイル。記事 ingestion 周期 (~30 分) に対し revalidate 1 分は十分
 * 新鮮。expire 1h は long tail traffic 用の上限。
 *
 * cache key は引数 `query` のシリアライズで決まる。`ArticleQuery` の shape
 * は callsite の `parseArticleQuery` で zod 検証通過後に常に同 shape で
 * 確定するため、`Object.entries` の挿入順序による cache pollution は
 * structural に防がれている。
 */
export async function getArticles(
  query?: ArticleQuery,
): Promise<PaginatedArticleResponse> {
  "use cache";
  cacheLife("minutes");
  const params = new URLSearchParams();
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return publicServerFetch<PaginatedArticleResponse>(
    `/articles${qs ? `?${qs}` : ""}`,
  );
}
