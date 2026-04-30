"use server";

import { updateTag } from "next/cache";
import { serverEmpty } from "@/lib/api/server-fetcher";
import { requireSessionForAction } from "@/lib/auth/guards";
import { PositiveIdSchema } from "@/lib/validation/id";
import { addToWatchlistCore } from "./watchlist-cores";

/** Add an article to the watchlist (Server Action). */
export async function addToWatchlist(articleId: number): Promise<void> {
  await requireSessionForAction();
  const validArticleId = PositiveIdSchema.parse(articleId);
  await addToWatchlistCore(validArticleId, serverEmpty);
  // Pattern B: per-user watchlist Set tag のみ無効化。`articles` cache は
  // user 非依存なので mutation で動かさない (本 user 以外への影響ゼロ)。
  // `updateTag` は Server Action 専用 idiom で immediate expiration、当該
  // tag を持つ data cache を同一リクエスト内で吹き飛ばし current route を
  // 再生成する (read-your-own-writes 即時確定)。
  updateTag("watchlist:me");
}
