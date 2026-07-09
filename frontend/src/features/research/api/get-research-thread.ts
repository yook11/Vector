import "@/lib/api/hey-api-interceptors";
import { getResearchThread as getResearchThreadSdk } from "@/types/sdk.gen";
import type { ResearchThreadDetail } from "@/types/types.gen";

export async function getResearchThread(
  threadId: string,
  fetcher: typeof getResearchThreadSdk = getResearchThreadSdk,
): Promise<ResearchThreadDetail> {
  const { data } = await fetcher({
    throwOnError: true,
    cache: "no-store",
    path: { thread_id: threadId },
  });
  return data;
}
