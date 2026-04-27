import { serverFetch } from "@/lib/api/server-fetcher";
import type { NewsSourceDetailList } from "@/types";

/** Fetch all news sources (SSR). */
export async function getSources(): Promise<NewsSourceDetailList> {
  return serverFetch<NewsSourceDetailList>("/admin/sources", {
    cache: "no-store",
  });
}
