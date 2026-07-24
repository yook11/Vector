import type { Metadata } from "next";
import { getResearchThreads, parseResearchLimit } from "@/features/research";
import { ResearchRouteModelCommit } from "@/features/research-client";
import { requireSession } from "@/lib/auth/guards";
import type { SearchParams } from "@/lib/types/route";

export const metadata: Metadata = {
  title: "Research | Vector",
};

interface ResearchPageProps {
  searchParams: Promise<SearchParams>;
}

export default async function ResearchPage({
  searchParams,
}: ResearchPageProps) {
  await requireSession();
  const raw = await searchParams;
  const limit = parseResearchLimit(raw);
  const threads = await getResearchThreads(limit);

  return (
    <ResearchRouteModelCommit threads={threads} thread={null} limit={limit} />
  );
}
