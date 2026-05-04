import { cacheLife } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import { listCategories } from "@/types/sdk.gen";
import type { CategoryDetailList } from "@/types/types.gen";

/** Fetch all categories with recent article counts (response is user-independent). */
export async function getCategories(): Promise<CategoryDetailList> {
  "use cache";
  cacheLife("hours");
  const { data } = await listCategories({
    client: publicClient,
    throwOnError: true,
  });
  return data;
}
