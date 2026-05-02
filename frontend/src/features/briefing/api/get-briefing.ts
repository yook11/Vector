import { cacheLife, cacheTag } from "next/cache";
import {
  type BriefingResponseParsed,
  BriefingResponseSchema,
} from "@/features/briefing/schemas/briefing";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import { briefingCategoryTag, cacheTags } from "@/lib/cache/tags";

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
  const raw = await publicServerFetch<unknown>(
    `/briefing/${encodeURIComponent(slug)}`,
  );
  return BriefingResponseSchema.parse(raw);
}
