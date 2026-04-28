import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { CategoryDetailListResponse } from "@/types";

/** Fetch all categories with recent article counts (response is user-independent). */
export async function getCategories(): Promise<CategoryDetailListResponse> {
  return publicServerFetch<CategoryDetailListResponse>("/categories", {
    next: { revalidate: 3600, tags: ["categories"] },
  });
}
