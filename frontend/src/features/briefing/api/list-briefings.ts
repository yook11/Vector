import { cacheLife, cacheTag } from "next/cache";
import {
  type BriefingListResponseParsed,
  BriefingListResponseSchema,
} from "@/features/briefing/schemas/briefing";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import { cacheTags } from "@/lib/cache/tags";

/**
 * 全カテゴリの最新 briefing 一覧を取得 (anonymous でも閲覧可能)。
 *
 * Hybrid 戦略:
 * - (a) `cacheLife("hours")` ISR backstop で最大 1 時間で必ず更新
 * - (b) backend (FrontendRevalidateNotifier) が生成成功時に
 *   `revalidateTag("briefing:list")` を打つ on-demand 経路
 * 二重にしているのは on-demand が落ちても backstop が拾うため。
 */
export async function listBriefings(): Promise<BriefingListResponseParsed> {
  "use cache";
  cacheLife("hours");
  cacheTag(cacheTags.briefingList);
  const raw = await publicServerFetch<unknown>("/briefing");
  return BriefingListResponseSchema.parse(raw);
}
