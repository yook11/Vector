import "@/lib/api/hey-api-interceptors";
import { listResearchThreads as listResearchThreadsSdk } from "@/types/sdk.gen";
import type { PaginatedResearchThreadResponse } from "@/types/types.gen";

export async function getResearchThreads(
  limit: number,
  fetcher: typeof listResearchThreadsSdk = listResearchThreadsSdk,
): Promise<PaginatedResearchThreadResponse> {
  const { data } = await fetcher({
    throwOnError: true,
    cache: "no-store",
    query: { page: 1, perPage: limit },
  });
  return data;
}
