"use server";

import "@/lib/api/hey-api-interceptors";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { requireSessionForAction } from "@/lib/auth/guards";
import { createResearchResponse as createResearchResponseSdk } from "@/types/sdk.gen";
import type { ResearchRunStartResponse } from "@/types/types.gen";
import {
  ResearchQuestionSchema,
  ResearchUuidSchema,
} from "../schemas/research";

export async function submitResearchQuestion(
  question: string,
  threadId?: string,
): Promise<ResearchRunStartResponse> {
  await requireSessionForAction();
  const parsedQuestion = ResearchQuestionSchema.parse(question);
  const parsedThreadId =
    threadId === undefined ? undefined : ResearchUuidSchema.parse(threadId);

  const { data } = await createResearchResponseSdk({
    throwOnError: true,
    cache: "no-store",
    body: {
      question: parsedQuestion,
      ...(parsedThreadId !== undefined ? { threadId: parsedThreadId } : {}),
    },
  });

  revalidatePath("/research");
  revalidatePath(`/research/${data.threadId}`);
  if (parsedThreadId === undefined) {
    redirect(`/research/${data.threadId}`);
  }
  return data;
}
