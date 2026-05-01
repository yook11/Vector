"use server";

import { updateTag } from "next/cache";
import { typedServer } from "@/lib/api/typed-server-fetcher";
import { requireSessionForAction } from "@/lib/auth/guards";
import { PositiveIdSchema } from "@/lib/validation/id";
import { removeFromWatchlistCore } from "./watchlist-cores";

/** Remove an article from the watchlist (Server Action). */
export async function removeFromWatchlist(articleId: number): Promise<void> {
  await requireSessionForAction();
  const validArticleId = PositiveIdSchema.parse(articleId);
  await removeFromWatchlistCore(validArticleId, typedServer);
  // Pattern B: per-user watchlist Set tag のみ無効化。
  // `updateTag` は Server Action 専用 idiom (詳細は add-to-watchlist 参照)。
  updateTag("watchlist:me");
}
