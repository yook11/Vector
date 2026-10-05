import { cacheLife } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import { getSimilarArticles as getSimilarArticlesSdk } from "@/types/sdk.gen";
import type { AnalyzedArticlePreview } from "@/types/types.gen";

/** Fetch articles semantically similar to the given article (response is user-independent). */
export async function getSimilarArticles(
  id: number,
  limit = 5,
): Promise<AnalyzedArticlePreview[]> {
  "use cache";
  cacheLife("hours");
  const { data } = await getSimilarArticlesSdk({
    client: publicClient,
    throwOnError: true,
    path: { article_id: id },
    query: { limit },
  });
  return data;
}
