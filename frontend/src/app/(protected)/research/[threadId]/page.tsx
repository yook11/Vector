import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";
import {
  getResearchThread,
  getResearchThreads,
  parseResearchLimit,
  ResearchUuidSchema,
  ResearchWorkspace,
} from "@/features/research";
import { ApiError } from "@/lib/api/error";
import { requireSession } from "@/lib/auth/guards";
import type { SearchParams } from "@/lib/types/route";
import type { ResearchThreadDetail } from "@/types/types.gen";

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
  let thread: ResearchThreadDetail;
  try {
    thread = await getResearchThread(parsedThreadId.data);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    throw err;
  }
  const threads = await getResearchThreads(limit);

  return (
    <PaperSurface>
      <ShellMasthead />
      <div className="relative min-h-dvh w-full overflow-hidden px-4 pb-6 md:px-6">
        <PaperTexture />
        <ResearchWorkspace threads={threads} thread={thread} limit={limit} />
      </div>
    </PaperSurface>
  );
}
