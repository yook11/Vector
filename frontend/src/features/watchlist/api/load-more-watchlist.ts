"use server";

import { requireSessionForAction } from "@/lib/auth/guards";
import { CursorSchema } from "@/lib/validation/cursor";
import type { AnalyzedArticlePreviewList } from "@/types/types.gen";
import { getWatchlist } from "./get-watchlist";

/** ウォッチリストの続きを読み込む (Server Action)。返す記事はすべてウォッチ済み。 */
export async function loadMoreWatchlist(cursor: string): Promise<{
  list: AnalyzedArticlePreviewList;
  watchedIds: Set<number>;
}> {
  await requireSessionForAction();
  const list = await getWatchlist(CursorSchema.parse(cursor));
  return { list, watchedIds: new Set(list.items.map((article) => article.id)) };
}
