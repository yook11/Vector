import { cacheLife, cacheTag } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import { cacheTags } from "@/lib/cache/tags";
import type { TrendsResponse } from "@/types";
import { getTrends as getTrendsSdk } from "@/types/sdk.gen";

/**
 * Fetch the latest trends snapshot (response is user-independent).
 *
 * Hybrid 戦略 (briefing と同じ):
 * - (a) `cacheLife("hours")` ISR backstop
 * - (b) backend (FrontendRevalidateNotifier) が生成成功後に
 *   `revalidateTag("trends")` で on-demand 更新
 */
export async function getTrends(): Promise<TrendsResponse> {
  "use cache";
  cacheLife("hours");
  cacheTag(cacheTags.trends);
  const { data } = await getTrendsSdk({
    client: publicClient,
    throwOnError: true,
  });
  return data;
}
