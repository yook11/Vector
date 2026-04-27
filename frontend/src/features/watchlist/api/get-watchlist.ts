import { serverFetch } from "@/lib/api/server-fetcher";
import type { PaginatedArticleResponse } from "@/types";

/** Fetch user's watchlist. */
export async function getWatchlist(
  page = 1,
  perPage = 20,
): Promise<PaginatedArticleResponse> {
  return serverFetch<PaginatedArticleResponse>(
    `/me/watchlist?page=${page}&perPage=${perPage}`,
    { cache: "no-store" },
  );
}
