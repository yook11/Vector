import { cacheLife } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import type { TrendsResponse } from "@/types";
import { getTrends as getTrendsSdk } from "@/types/sdk.gen";

/** Fetch the latest trends snapshot (response is user-independent). */
export async function getTrends(): Promise<TrendsResponse> {
  "use cache";
  cacheLife("days");
  const { data } = await getTrendsSdk({
    client: publicClient,
    throwOnError: true,
  });
  return data;
}
