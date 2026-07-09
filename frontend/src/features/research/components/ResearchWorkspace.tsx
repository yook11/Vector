import type {
  PaginatedResearchThreadResponse,
  ResearchThreadDetail,
} from "@/types/types.gen";
import { ResearchSidebar } from "./ResearchSidebar";
import { ResearchEmptyView, ResearchThreadView } from "./ResearchThreadView";

interface ResearchWorkspaceProps {
  threads: PaginatedResearchThreadResponse;
  thread: ResearchThreadDetail | null;
  limit: number;
}

export function ResearchWorkspace({
  threads,
  thread,
  limit,
}: ResearchWorkspaceProps) {
  return (
    <main className="relative z-10 mx-auto flex h-[calc(100dvh-5.5rem)] w-full min-w-0 max-w-[1280px] flex-col overflow-hidden border-x border-b border-[var(--vector-rule)] bg-[var(--vector-surface)] md:flex-row">
      <ResearchSidebar
        threads={threads}
        activeThreadId={thread?.threadId}
        limit={limit}
      />
      {thread ? <ResearchThreadView thread={thread} /> : <ResearchEmptyView />}
    </main>
  );
}
