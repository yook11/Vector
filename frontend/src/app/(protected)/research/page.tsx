import type { Metadata } from "next";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";
import {
  getResearchThreads,
  parseResearchLimit,
  ResearchWorkspace,
} from "@/features/research";
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
    <PaperSurface>
      <ShellMasthead />
      <div className="relative min-h-dvh w-full overflow-hidden px-4 pb-6 md:px-6">
        <PaperTexture />
        <ResearchWorkspace threads={threads} thread={null} limit={limit} />
      </div>
    </PaperSurface>
  );
}
