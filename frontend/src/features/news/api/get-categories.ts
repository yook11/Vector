import { cacheLife } from "next/cache";
import { apiCall, typedPublic } from "@/lib/api/typed-server-fetcher";
import type { CategoryDetailListResponse } from "@/types";

/** Fetch all categories with recent article counts (response is user-independent). */
export async function getCategories(): Promise<CategoryDetailListResponse> {
  "use cache";
  cacheLife("hours");
  return apiCall(typedPublic.GET("/api/v1/categories", {}));
}
