import { serverFetch } from "@/lib/api/server-fetcher";
import type { WeeklyTrendsResponse } from "@/types";

/** Fetch the latest weekly trends snapshot (or null state if not yet generated). */
export async function getWeeklyTrends(): Promise<WeeklyTrendsResponse> {
  return serverFetch<WeeklyTrendsResponse>("/weekly-trends", {
    next: { revalidate: 86400 },
  });
}
