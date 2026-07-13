import type {
  PaginatedResearchThreadResponse,
  ResearchThreadDetail,
} from "@/types/types.gen";
import { ResearchNavigationBoundary } from "./ResearchNavigationBoundary";
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
    <ResearchNavigationBoundary
      sidebar={
        <ResearchSidebar
          threads={threads}
          activeThreadId={thread?.threadId}
          limit={limit}
        />
      }
    >
      {thread ? (
        <ResearchThreadView thread={thread} withSourcesPanel />
      ) : (
        <ResearchEmptyView />
      )}
    </ResearchNavigationBoundary>
  );
}
