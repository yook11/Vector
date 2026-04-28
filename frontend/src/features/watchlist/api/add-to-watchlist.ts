"use server";

import { serverFetch } from "@/lib/api/server-fetcher";
import { requireSessionForAction } from "@/lib/auth/guards";

/** Add an article to the watchlist (Server Action). */
export async function addToWatchlist(articleId: number): Promise<void> {
  await requireSessionForAction();
  await serverFetch<void>("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ articleId }),
  });
  // getWatchlist は user-specific で no-store のため tag invalidation 不要。
  // 一覧の isWatched フラグは per-user キャッシュ TTL までは stale だが、
  // optimistic UI が即時反映するので体感上は問題ない。
}
