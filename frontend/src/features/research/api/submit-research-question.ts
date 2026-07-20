"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { requireSessionForAction } from "@/lib/auth/guards";
import {
  ResearchQuestionSchema,
  ResearchUuidSchema,
} from "../schemas/research";
import {
  type SubmitResearchQuestionResult,
  submitResearchQuestionCore,
} from "./submit-research-question-core";

export async function submitResearchQuestion(
  question: string,
  threadId?: string,
): Promise<SubmitResearchQuestionResult> {
  await requireSessionForAction();
  const parsedQuestion = ResearchQuestionSchema.parse(question);
  const parsedThreadId =
    threadId === undefined ? undefined : ResearchUuidSchema.parse(threadId);

  const result = await submitResearchQuestionCore({
    question: parsedQuestion,
    ...(parsedThreadId !== undefined ? { threadId: parsedThreadId } : {}),
  });
  if (result.kind === "daily-request-limit-exceeded") return result;

  revalidatePath("/research");
  revalidatePath(`/research/${result.run.threadId}`);
  if (parsedThreadId === undefined) {
    redirect(`/research/${result.run.threadId}`);
  }
  return result;
}
