"use client";

import { clientFetch } from "@/lib/api/client-fetcher";

/** Add an article to the watchlist. */
export async function addToWatchlist(articleId: number): Promise<void> {
  await clientFetch("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ articleId }),
  });
}
