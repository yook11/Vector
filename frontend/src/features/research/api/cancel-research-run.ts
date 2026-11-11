"use server";

import "@/lib/api/hey-api-interceptors";
import { revalidatePath } from "next/cache";
import { requireSessionForAction } from "@/lib/auth/guards";
import { cancelResearchRun as cancelResearchRunSdk } from "@/types/sdk.gen";
import { ResearchUuidSchema } from "../schemas/research";

export async function cancelResearchRun(
  runId: string,
  threadId?: string,
): Promise<void> {
  await requireSessionForAction();
  const parsedRunId = ResearchUuidSchema.parse(runId);
  const parsedThreadId =
    threadId === undefined ? undefined : ResearchUuidSchema.parse(threadId);
  await cancelResearchRunSdk({
    throwOnError: true,
    cache: "no-store",
    path: { run_id: parsedRunId },
  });
  revalidatePath("/research");
  if (parsedThreadId !== undefined) {
    revalidatePath(`/research/${parsedThreadId}`);
  }
}
