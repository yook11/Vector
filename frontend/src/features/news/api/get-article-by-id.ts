import { cacheLife } from "next/cache";
import { ApiError } from "@/lib/api/error";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import { getArticle } from "@/types/sdk.gen";
import type { ArticleDetail } from "@/types/types.gen";

/**
 * 記事詳細取得 (response は user 非依存)。
 *
 * `publicClient` + `'use cache'` で全 user 共有 cache に乗せる。session を
 * 読まず BFF 経由証明だけを付ける client なので cookies/headers を踏まずに
 * cache 内で安全に呼べる。ウォッチ状態は呼び出し側 page で `getWatchlistIds` と
 * `Promise.all` し、`Set.has` で merge する。
 * cache 境界を越える前に 404/410 を `null` へ変換し、Error の class identity
 * に依存せず not-found を判定できるようにする。
 */
export async function getArticleById(
  id: number,
): Promise<ArticleDetail | null> {
  "use cache";
  cacheLife("hours");
  try {
    const { data } = await getArticle({
      client: publicClient,
      throwOnError: true,
      path: { article_id: id },
    });
    return data;
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
      return null;
    }
    throw err;
  }
}
