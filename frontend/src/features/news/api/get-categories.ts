import { serverFetch } from "@/lib/api/server-fetcher";
import type { CategoryDetailListResponse } from "@/types";

/** Fetch all categories with recent article counts. */
export async function getCategories(): Promise<CategoryDetailListResponse> {
  return serverFetch<CategoryDetailListResponse>("/categories", {
    cache: "no-store",
  });
}
