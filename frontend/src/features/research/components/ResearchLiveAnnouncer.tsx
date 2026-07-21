"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

interface ReportedLiveAnnouncement {
  threadId: string;
  runId: string;
  text: string;
}

interface ResearchLiveAnnouncementContextValue {
  reported: ReportedLiveAnnouncement | null;
  report: ((runId: string, text: string) => void) | null;
}

const ResearchLiveAnnouncementContext =
  createContext<ResearchLiveAnnouncementContextValue>({
    reported: null,
    report: null,
  });

const ResearchLiveAnnouncementOwnerContext = createContext<
  ((announcement: string) => void) | null
>(null);

interface ResearchLiveAnnouncementOwnerProps {
  report: (announcement: string) => void;
  children: ReactNode;
}

export function ResearchLiveAnnouncementOwner({
  report,
  children,
}: ResearchLiveAnnouncementOwnerProps) {
  return (
    <ResearchLiveAnnouncementOwnerContext.Provider value={report}>
      {children}
    </ResearchLiveAnnouncementOwnerContext.Provider>
  );
}

interface ResearchLiveAnnouncementBoundaryProps {
  threadId: string;
  children: ReactNode;
}

export function ResearchLiveAnnouncementBoundary({
  threadId,
  children,
}: ResearchLiveAnnouncementBoundaryProps) {
  const [reported, setReported] = useState<ReportedLiveAnnouncement | null>(
    null,
  );
  const report = useCallback(
    (runId: string, text: string) => {
      setReported((current) => {
        if (
          current?.threadId === threadId &&
          current.runId === runId &&
          current.text === text
        ) {
          return current;
        }
        return { threadId, runId, text };
      });
    },
    [threadId],
  );
  const value = useMemo<ResearchLiveAnnouncementContextValue>(
    () => ({
      reported: reported?.threadId === threadId ? reported : null,
      report,
    }),
    [report, reported, threadId],
  );

  return (
    <ResearchLiveAnnouncementContext.Provider value={value}>
      {children}
    </ResearchLiveAnnouncementContext.Provider>
  );
}

export function useResearchLiveAnnouncementReporter() {
  return useContext(ResearchLiveAnnouncementContext).report;
}

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
  const { reported } = useContext(ResearchLiveAnnouncementContext);
  const externalOwner = useContext(ResearchLiveAnnouncementOwnerContext);
  const currentThreadId = useRef(threadId);
  const observedActiveRunIds = useRef(new Set<string>());
  const announcedRunIds = useRef(new Set<string>());
  const [completedAnnouncement, setCompletedAnnouncement] = useState<{
    threadId: string;
    runId: string;
  } | null>(null);

  const isCurrentThread = currentThreadId.current === threadId;
  const pendingCompletedRunId =
    isCurrentThread && activeRunId === null
      ? completedRunIds.find(
          (runId) =>
            observedActiveRunIds.current.has(runId) &&
            !announcedRunIds.current.has(runId),
        )
      : undefined;

  useEffect(() => {
    if (currentThreadId.current !== threadId) {
      currentThreadId.current = threadId;
      observedActiveRunIds.current.clear();
      announcedRunIds.current.clear();
      setCompletedAnnouncement(null);
    }

    if (activeRunId !== null) {
      observedActiveRunIds.current.add(activeRunId);
      setCompletedAnnouncement(null);
      return;
    }

    const completedRunId = completedRunIds.find(
      (runId) =>
        observedActiveRunIds.current.has(runId) &&
        !announcedRunIds.current.has(runId),
    );
    if (completedRunId === undefined) return;

    announcedRunIds.current.add(completedRunId);
    setCompletedAnnouncement({ threadId, runId: completedRunId });
  }, [activeRunId, completedRunIds, threadId]);

  let announcement = "";
  if (
    activeRunId !== null &&
    reported?.threadId === threadId &&
    reported.runId === activeRunId &&
    reported.text.length > 0
  ) {
    announcement = `進行状況: ${reported.text}`;
  } else if (
    pendingCompletedRunId !== undefined ||
    completedAnnouncement?.threadId === threadId
  ) {
    announcement = "回答が完了しました";
  }

  useEffect(() => {
    externalOwner?.(announcement);
  }, [announcement, externalOwner]);

  useEffect(
    () => () => {
      externalOwner?.("");
    },
    [externalOwner],
  );

  if (externalOwner !== null) return null;

  return (
    <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {announcement}
    </p>
  );
}
