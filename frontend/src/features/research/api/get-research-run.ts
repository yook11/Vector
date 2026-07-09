import "@/lib/api/hey-api-interceptors";
import { getResearchRun as getResearchRunSdk } from "@/types/sdk.gen";
import type { ResearchRunResponse } from "@/types/types.gen";

export async function getResearchRun(
  runId: string,
  fetcher: typeof getResearchRunSdk = getResearchRunSdk,
): Promise<ResearchRunResponse> {
  const { data } = await fetcher({
    throwOnError: true,
    cache: "no-store",
    path: { run_id: runId },
  });
  return data;
}
