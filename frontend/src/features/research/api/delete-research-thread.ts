"use server";

import "@/lib/api/hey-api-interceptors";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { ApiError } from "@/lib/api/error";
import { requireSessionForAction } from "@/lib/auth/guards";
import { deleteResearchThread as deleteResearchThreadSdk } from "@/types/sdk.gen";
import { ResearchUuidSchema } from "../schemas/research";

export async function deleteResearchThread(threadId: string): Promise<never> {
  await requireSessionForAction();
  const parsedThreadId = ResearchUuidSchema.parse(threadId);
  try {
    await deleteResearchThreadSdk({
      throwOnError: true,
      cache: "no-store",
      path: { thread_id: parsedThreadId },
    });
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 404) {
      throw err;
    }
  }
  revalidatePath("/research");
  redirect("/research");
}
