import { serverFetch } from "@/lib/api/server-fetcher";
import type { ArticleDetail } from "@/types";

/** Fetch a single article by ID. */
export async function getArticleById(id: number): Promise<ArticleDetail> {
  return serverFetch<ArticleDetail>(`/articles/${id}`, {
    next: { revalidate: 1800, tags: ["articles", `article:${id}`] },
  });
}
