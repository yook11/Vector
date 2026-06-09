import { cacheLife, cacheTag } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import { briefingCategoryTag, cacheTags } from "@/lib/cache/tags";
import { getLatestBriefing } from "@/types/sdk.gen";
import {
  type BriefingResponseParsed,
  BriefingResponseSchema,
} from "../schemas/briefing";

/**
 * 指定カテゴリの最新 briefing 詳細を取得 (anonymous でも閲覧可能)。
 *
 * Hybrid 戦略 (一覧と同じ):
 * - (a) `cacheLife("hours")` ISR backstop
 * - (b) backend (FrontendRevalidateNotifier) が
 *   `revalidateTag("briefing:<slug>")` で on-demand 更新
 */
export async function getBriefing(
  slug: string,
): Promise<BriefingResponseParsed> {
  "use cache";
  cacheLife("hours");
  cacheTag(briefingCategoryTag(slug));
  cacheTag(cacheTags.briefingList);
  const { data } = await getLatestBriefing({
    client: publicClient,
    throwOnError: true,
    path: { category_slug: slug },
  });
  return BriefingResponseSchema.parse(data);
}
