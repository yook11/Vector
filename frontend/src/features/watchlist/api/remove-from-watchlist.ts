"use server";

import { refresh, updateTag } from "next/cache";
import { serverEmpty } from "@/lib/api/server-fetcher";
import { requireSessionForAction } from "@/lib/auth/guards";
import { removeFromWatchlistCore } from "./watchlist-cores";

/** Remove an article from the watchlist (Server Action). */
export async function removeFromWatchlist(articleId: number): Promise<void> {
  await requireSessionForAction();
  await removeFromWatchlistCore(articleId, serverEmpty);
  // Pattern B: per-user watchlist Set tag のみ無効化。
  // `updateTag` は Server Action 専用 idiom (詳細は add-to-watchlist 参照)。
  updateTag("watchlist:me");
  // cacheComponents 有効時は Server Action 完了で current route の自動再
  // フェッチが行われない。明示的に client cache を refresh して
  // `getWatchlistIds` を再評価する (詳細は add-to-watchlist の同コメント)。
  refresh();
}
