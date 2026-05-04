import { cacheLife } from "next/cache";
import { publicClient } from "@/lib/api/hey-api-interceptors";
import type { WeeklyTrendsResponse } from "@/types";
import { getWeeklyTrends as getWeeklyTrendsSdk } from "@/types/sdk.gen";

/** Fetch the latest weekly trends snapshot (response is user-independent). */
export async function getWeeklyTrends(): Promise<WeeklyTrendsResponse> {
  "use cache";
  cacheLife("days");
  const { data } = await getWeeklyTrendsSdk({
    client: publicClient,
    throwOnError: true,
  });
  return data;
}
