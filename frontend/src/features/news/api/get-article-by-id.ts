import { cacheLife, cacheTag } from "next/cache";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { ArticleDetail } from "@/types";

/**
 * Fetch a single article by ID.
 *
 * Pattern B: response は user 非依存。`publicServerFetch` + `'use cache'`
 * で全 user 共有。ウォッチ状態は呼び出し側 page で `getWatchlistIds` と
 * `Promise.all` し、Set.has で merge する。
 */
export async function getArticleById(id: number): Promise<ArticleDetail> {
  "use cache";
  cacheLife("hours");
  cacheTag("articles", `article:${id}`);
  return publicServerFetch<ArticleDetail>(`/articles/${id}`);
}
