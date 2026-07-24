"use client";

import { useLayoutEffect, useMemo } from "react";
import type { ResearchThreadDetail } from "@/types/types.gen";
import { selectCommittedResearchRunIds } from "../selectors/research-runs";
import { useResearchSubmission } from "./ResearchSubmissionBoundary";

export function ResearchModelCommitReporter({
  thread,
}: {
  thread: ResearchThreadDetail | null;
}) {
  const { reportModelCommit } = useResearchSubmission();
  const threadId = thread?.threadId ?? null;
  const committedRunIds = useMemo(
    () => selectCommittedResearchRunIds(thread?.messages ?? []),
    [thread],
  );

  useLayoutEffect(() => {
    reportModelCommit({ threadId, committedRunIds });
  }, [committedRunIds, reportModelCommit, threadId]);

  return null;
}
