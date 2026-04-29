"use server";

import { revalidateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireSessionForAction } from "@/lib/auth/guards";

/** Add an article to the watchlist (Server Action). */
export async function addToWatchlist(articleId: number): Promise<void> {
  await requireSessionForAction();
  await serverFetch<void>("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ articleId }),
  });
  // Pattern B: per-user watchlist Set tag のみ無効化。`articles` cache は
  // user 非依存なので mutation で動かさない (本 user 以外への影響ゼロ)。
  revalidateTag("watchlist:me", "max");
}
