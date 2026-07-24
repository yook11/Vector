import type { Metadata } from "next";
import { notFound } from "next/navigation";
import {
  loadResearchThreadPage,
  parseResearchLimit,
  ResearchUuidSchema,
} from "@/features/research";
import { ResearchRouteModelCommit } from "@/features/research-client";
import { requireSession } from "@/lib/auth/guards";
import type { SearchParams } from "@/lib/types/route";

export const metadata: Metadata = {
  title: "Research Thread | Vector",
};

interface ResearchThreadPageProps {
  params: Promise<{ threadId: string }>;
  searchParams: Promise<SearchParams>;
}

export default async function ResearchThreadPage({
  params,
  searchParams,
}: ResearchThreadPageProps) {
  await requireSession();
  const [{ threadId }, rawSearchParams] = await Promise.all([
    params,
    searchParams,
  ]);
  const parsedThreadId = ResearchUuidSchema.safeParse(threadId);
  if (!parsedThreadId.success) {
    notFound();
  }
  const limit = parseResearchLimit(rawSearchParams);
  const model = await loadResearchThreadPage(parsedThreadId.data, limit);
  if (model.state === "not-found") {
    notFound();
  }

  return (
    <ResearchRouteModelCommit
      threads={model.threads}
      thread={model.thread}
      limit={limit}
    />
  );
}
