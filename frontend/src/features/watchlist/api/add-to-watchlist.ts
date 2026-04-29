"use server";

import { refresh, revalidateTag } from "next/cache";
import { serverEmpty } from "@/lib/api/server-fetcher";
import { requireSessionForAction } from "@/lib/auth/guards";
import { addToWatchlistCore } from "./watchlist-cores";

/** Add an article to the watchlist (Server Action). */
export async function addToWatchlist(articleId: number): Promise<void> {
  await requireSessionForAction();
  await addToWatchlistCore(articleId, serverEmpty);
  // Pattern B: per-user watchlist Set tag のみ無効化。`articles` cache は
  // user 非依存なので mutation で動かさない (本 user 以外への影響ゼロ)。
  revalidateTag("watchlist:me", "max");
  // cacheComponents 有効時は Server Action 完了で current route の自動再
  // フェッチが行われない。明示的に client cache を refresh して `getWatchlistIds`
  // を再評価し、`useOptimistic` が base に戻る前に親が新しい `watchedIds` を
  // 渡せるようにする。これがないと button が一旦元の状態に flicker し、
  // ユーザーが再クリックして 409/404 を量産する。
  refresh();
}
