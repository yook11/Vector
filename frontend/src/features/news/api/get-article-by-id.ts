import { cacheLife } from "next/cache";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { ArticleDetail } from "@/types";

/**
 * 記事詳細取得 (response は user 非依存)。
 *
 * `publicServerFetch` + `'use cache'` で全 user 共有 cache に乗せる。
 * ウォッチ状態は呼び出し側 page で `getWatchlistIds` と `Promise.all` し、
 * `Set.has` で merge する。
 */
export async function getArticleById(id: number): Promise<ArticleDetail> {
  "use cache";
  cacheLife("hours");
  return publicServerFetch<ArticleDetail>(`/articles/${id}`);
}
