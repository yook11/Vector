import { cacheLife } from "next/cache";
import { apiCall, typedPublic } from "@/lib/api/typed-server-fetcher";
import type { CategoryDetailList } from "@/types/types.gen";

/** Fetch all categories with recent article counts (response is user-independent). */
export async function getCategories(): Promise<CategoryDetailList> {
  "use cache";
  cacheLife("hours");
  return apiCall(typedPublic.GET("/api/v1/categories", {}));
}
