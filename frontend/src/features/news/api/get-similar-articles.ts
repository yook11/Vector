import { cacheLife } from "next/cache";
import { apiCall, typedPublic } from "@/lib/api/typed-server-fetcher";
import type { ArticleBrief } from "@/types/types.gen";

/** Fetch articles semantically similar to the given article (response is user-independent). */
export async function getSimilarArticles(
  id: number,
  limit = 5,
): Promise<ArticleBrief[]> {
  "use cache";
  cacheLife("hours");
  return apiCall(
    typedPublic.GET("/api/v1/articles/{article_id}/similar", {
      params: { path: { article_id: id }, query: { limit } },
    }),
  );
}
