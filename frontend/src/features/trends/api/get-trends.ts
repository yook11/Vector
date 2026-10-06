import { cacheLife, cacheTag } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import { cacheTags } from "@/lib/cache/tags";
import { getTrends as getTrendsSdk } from "@/types/sdk.gen";
import type { Trends } from "@/types/types.gen";

/**
 * Fetch the latest trends snapshot (response is user-independent).
 * 1件も生成されていなければ null。
 *
 * Hybrid 戦略 (briefing と同じ):
 * - (a) `cacheLife("hours")` ISR backstop
 * - (b) backend (FrontendRevalidateNotifier) が生成成功後に
 *   `revalidateTag("trends")` で on-demand 更新
 */
export async function getTrends(): Promise<Trends | null> {
  "use cache";
  cacheLife("hours");
  cacheTag(cacheTags.trends);
  const { data } = await getTrendsSdk({
    client: publicClient,
    throwOnError: true,
  });
  return data;
}
