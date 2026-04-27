"use client";

import { clientFetch } from "@/lib/api/client-fetcher";

/** Remove an article from the watchlist. */
export async function removeFromWatchlist(articleId: number): Promise<void> {
  await clientFetch(`/me/watchlist/${articleId}`, {
    method: "DELETE",
  });
}
