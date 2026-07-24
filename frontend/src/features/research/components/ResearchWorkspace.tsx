import type {
  PaginatedResearchThreadResponse,
  ResearchThreadDetail,
} from "@/types/types.gen";
import { selectActiveResearchRunId } from "../selectors/research-runs";
import { ResearchComposer } from "./ResearchComposer";
import { ResearchModelCommitReporter } from "./ResearchModelCommitReporter";
import { ResearchNavigationBoundary } from "./ResearchNavigationBoundary";
import { ResearchSidebar } from "./ResearchSidebar";
import { ResearchWorkspacePanel } from "./ResearchThreadView";

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
  const currentRunId = selectActiveResearchRunId(thread?.messages ?? []);

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
      <ResearchModelCommitReporter thread={thread} />
      <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-[var(--vector-surface-2)]">
        <ResearchWorkspacePanel
          thread={thread}
          composer={
            <ResearchComposer
              threadId={thread?.threadId}
              activeRunId={currentRunId}
            />
          }
        />
      </section>
    </ResearchNavigationBoundary>
  );
}
