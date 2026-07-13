"use client";

import { useEffect, useRef, useState } from "react";

interface ResearchLiveAnnouncerProps {
  threadId: string;
  activeRunId: string | null;
  completedRunIds: readonly string[];
}

export function ResearchLiveAnnouncer({
  threadId,
  activeRunId,
  completedRunIds,
}: ResearchLiveAnnouncerProps) {
  return (
    <ThreadResearchLiveAnnouncer
      key={threadId}
      activeRunId={activeRunId}
      completedRunIds={completedRunIds}
    />
  );
}

type ThreadResearchLiveAnnouncerProps = Omit<
  ResearchLiveAnnouncerProps,
  "threadId"
>;

function ThreadResearchLiveAnnouncer({
  activeRunId,
  completedRunIds,
}: ThreadResearchLiveAnnouncerProps) {
  const observedActiveRunIds = useRef(new Set<string>());
  const announcedRunIds = useRef(new Set<string>());
  const [announcementRunId, setAnnouncementRunId] = useState<string | null>(
    null,
  );

  useEffect(() => {
    if (activeRunId !== null) {
      observedActiveRunIds.current.add(activeRunId);
      setAnnouncementRunId(null);
      return;
    }

    const completedRunId = completedRunIds.find(
      (runId) =>
        observedActiveRunIds.current.has(runId) &&
        !announcedRunIds.current.has(runId),
    );
    if (completedRunId === undefined) return;

    announcedRunIds.current.add(completedRunId);
    setAnnouncementRunId(completedRunId);
  }, [activeRunId, completedRunIds]);

  if (announcementRunId === null) return null;
  return (
    <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      回答が完了しました
    </p>
  );
}
