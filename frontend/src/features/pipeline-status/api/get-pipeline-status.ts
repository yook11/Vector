import "@/lib/api/hey-api-interceptors";
import { getPipelineHealth as getPipelineHealthSdk } from "@/types/sdk.gen";
import type { PipelineHealthResponse } from "@/types/types.gen";

/**
 * pipeline 各 stage の健全性スナップショットを取得する (admin only, SSR)。
 *
 * 運用確認用途なので最新値を優先し `cache: "no-store"` で取得する
 * (auth 依存 endpoint なので `'use cache'` は使えない)。auth header は
 * side-effect import した interceptor の singleton `client` が注入する。
 *
 * fetcher は default 引数で受けるため、生成 SDK を差し替えてテストから
 * 呼び出し形 (`throwOnError` / `cache`) を固定できる。`{ throwOnError: true }`
 * で戻り値の `data` が `T | undefined` ではなく `T` に narrow される。
 */
export async function getPipelineStatus(
  fetcher: typeof getPipelineHealthSdk = getPipelineHealthSdk,
): Promise<PipelineHealthResponse> {
  const { data } = await fetcher({ throwOnError: true, cache: "no-store" });
  return data;
}
