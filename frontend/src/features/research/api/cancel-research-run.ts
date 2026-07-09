"use server";

import "@/lib/api/hey-api-interceptors";
import { revalidatePath } from "next/cache";
import { ApiError } from "@/lib/api/error";
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
  try {
    await cancelResearchRunSdk({
      throwOnError: true,
      cache: "no-store",
      path: { run_id: parsedRunId },
    });
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 409) {
      throw err;
    }
  }
  revalidatePath("/research");
  if (parsedThreadId !== undefined) {
    revalidatePath(`/research/${parsedThreadId}`);
  }
}
