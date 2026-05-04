import { cacheLife } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import { listCategories } from "@/types/sdk.gen";
import type { CategoryDetailList } from "@/types/types.gen";

/**
 * Fetch all categories with recent article counts (response is user-independent).
 *
 * `recentCount` は backend で「直近 24 時間に AI 分類が完了した記事数」として
 * 算出されている rolling window 値。`cacheLife("minutes")` (stale 5min /
 * revalidate 1min / expire 1h) を採用することで、ingestion (~30 分周期) や
 * 24h window から漏れる記事に対してサイドバー表示が大幅にずれない粒度に
 * 揃える。`getArticles` と同プロファイル。slug/name 自体は不変なので
 * minutes 粒度で十分。
 */
export async function getCategories(): Promise<CategoryDetailList> {
  "use cache";
  cacheLife("minutes");
  const { data } = await listCategories({
    client: publicClient,
    throwOnError: true,
  });
  return data;
}
