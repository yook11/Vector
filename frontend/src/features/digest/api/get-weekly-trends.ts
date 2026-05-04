import { cacheLife } from "next/cache";
import { apiCall, typedPublic } from "@/lib/api/typed-server-fetcher";
import type { WeeklyTrendsResponse } from "@/types";

/** Fetch the latest weekly trends snapshot (response is user-independent). */
export async function getWeeklyTrends(): Promise<WeeklyTrendsResponse> {
  "use cache";
  cacheLife("days");
  return apiCall(typedPublic.GET("/api/v1/weekly-trends", {}));
}
