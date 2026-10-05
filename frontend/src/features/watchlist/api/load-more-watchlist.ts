"use server";

import { requireSessionForAction } from "@/lib/auth/guards";
import { CursorSchema } from "@/lib/validation/cursor";
import type { AnalyzedArticlePreviewList } from "@/types/types.gen";
import { getWatchlist } from "./get-watchlist";

/** ウォッチリストの続きを読み込む (Server Action)。 */
export async function loadMoreWatchlist(
  cursor: string,
): Promise<AnalyzedArticlePreviewList> {
  await requireSessionForAction();
  return getWatchlist(CursorSchema.parse(cursor));
}
