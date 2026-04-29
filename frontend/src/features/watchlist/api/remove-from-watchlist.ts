"use server";

import { refresh, revalidateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireSessionForAction } from "@/lib/auth/guards";

/** Remove an article from the watchlist (Server Action). */
export async function removeFromWatchlist(articleId: number): Promise<void> {
  await requireSessionForAction();
  await serverFetch<void>(`/me/watchlist/${articleId}`, {
    method: "DELETE",
  });
  // Pattern B: per-user watchlist Set tag のみ無効化。
  revalidateTag("watchlist:me", "max");
  // cacheComponents 有効時は Server Action 完了で current route の自動再
  // フェッチが行われない。明示的に client cache を refresh して
  // `getWatchlistIds` を再評価する (詳細は add-to-watchlist の同コメント)。
  refresh();
}
