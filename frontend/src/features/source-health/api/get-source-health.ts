import "@/lib/api/hey-api-interceptors";
import { getSourceHealth as getSourceHealthSdk } from "@/types/sdk.gen";
import type { SourceHealthResponse, WindowHours } from "@/types/types.gen";

/**
 * ニュースソース別の取得・分析可能化 health を取得する (admin only, SSR)。
 *
 * 運用確認用途なので最新値を優先し `cache: "no-store"` で取得する (auth 依存
 * endpoint なので `'use cache'` は使えない)。auth header は side-effect import した
 * interceptor の singleton `client` が注入する。
 *
 * `windowHours` は呼び出し側 (page-model) で label から変換済みの許可値を渡す。
 * fetcher は default 引数で受け、生成 SDK を差し替えてテストから呼び出し形を固定できる。
 * `{ throwOnError: true }` で戻り値の `data` が `T` に narrow される。
 */
export async function getSourceHealth(
  windowHours: WindowHours,
  fetcher: typeof getSourceHealthSdk = getSourceHealthSdk,
): Promise<SourceHealthResponse> {
  const { data } = await fetcher({
    throwOnError: true,
    cache: "no-store",
    query: { windowHours },
  });
  return data;
}
