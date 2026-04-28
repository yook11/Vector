import { cacheLife, cacheTag } from "next/cache";
import { publicServerFetch } from "@/lib/api/server-fetcher";
import type { WeeklyTrendsResponse } from "@/types";

/** Fetch the latest weekly trends snapshot (response is user-independent). */
export async function getWeeklyTrends(): Promise<WeeklyTrendsResponse> {
  "use cache";
  cacheLife("days");
  cacheTag("weekly-trends");
  return publicServerFetch<WeeklyTrendsResponse>("/weekly-trends");
}
