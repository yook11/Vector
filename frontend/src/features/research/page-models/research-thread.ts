import { ApiError } from "@/lib/api/error";
import type {
  PaginatedResearchThreadResponse,
  ResearchThreadDetail,
} from "@/types/types.gen";
import { getResearchThread } from "../api/get-research-thread";
import { getResearchThreads } from "../api/get-research-threads";

type ResearchThreadPageModel =
  | { state: "not-found" }
  | {
      state: "ready";
      thread: ResearchThreadDetail;
      threads: PaginatedResearchThreadResponse;
    };

export async function loadResearchThreadPage(
  threadId: string,
  limit: number,
): Promise<ResearchThreadPageModel> {
  const [detailResult, listResult] = await Promise.allSettled([
    getResearchThread(threadId),
    getResearchThreads(limit),
  ]);

  if (detailResult.status === "rejected") {
    if (
      detailResult.reason instanceof ApiError &&
      detailResult.reason.status === 404
    ) {
      return { state: "not-found" };
    }
    throw detailResult.reason;
  }
  if (listResult.status === "rejected") {
    throw listResult.reason;
  }
  return {
    state: "ready",
    thread: detailResult.value,
    threads: listResult.value,
  };
}
