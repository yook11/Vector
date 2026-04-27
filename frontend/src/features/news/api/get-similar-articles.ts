import { serverFetch } from "@/lib/api/server-fetcher";
import type { ArticleBrief } from "@/types";

/** Fetch articles semantically similar to the given article. */
export async function getSimilarArticles(
  id: number,
  limit = 5,
): Promise<ArticleBrief[]> {
  return serverFetch<ArticleBrief[]>(`/articles/${id}/similar?limit=${limit}`, {
    cache: "no-store",
  });
}
