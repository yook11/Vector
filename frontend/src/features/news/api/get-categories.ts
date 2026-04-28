import { cacheLife, cacheTag } from "next/cache";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { CategoryDetailListResponse } from "@/types";

/** Fetch all categories with recent article counts (response is user-independent). */
export async function getCategories(): Promise<CategoryDetailListResponse> {
  "use cache";
  cacheLife("hours");
  cacheTag("categories");
  return publicServerFetch<CategoryDetailListResponse>("/categories");
}
