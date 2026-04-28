import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { ArticleBrief } from "@/types";

/** Fetch articles semantically similar to the given article (response is user-independent). */
export async function getSimilarArticles(
  id: number,
  limit = 5,
): Promise<ArticleBrief[]> {
  return publicServerFetch<ArticleBrief[]>(
    `/articles/${id}/similar?limit=${limit}`,
    { next: { revalidate: 3600, tags: ["articles"] } },
  );
}
