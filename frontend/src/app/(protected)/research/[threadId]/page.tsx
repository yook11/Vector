import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";
import {
  loadResearchThreadPage,
  parseResearchLimit,
  ResearchUuidSchema,
  ResearchWorkspace,
} from "@/features/research";
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
    <PaperSurface className="flex h-dvh min-h-0 flex-col overflow-hidden [&>header]:shrink-0">
      <ShellMasthead />
      <div className="relative flex min-h-0 w-full flex-1 overflow-hidden">
        <PaperTexture />
        <ResearchWorkspace
          threads={model.threads}
          thread={model.thread}
          limit={limit}
        />
      </div>
    </PaperSurface>
  );
}
