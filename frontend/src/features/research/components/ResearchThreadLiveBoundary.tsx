"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import type { ResearchMessageRun } from "@/types/types.gen";
import { useResearchRunLiveState } from "../hooks/useResearchRunLiveState";
import type { ResearchRunLiveSnapshot } from "../live/controller";
import { ActiveRunStatus } from "./ActiveRunStatus";
import { LiveAnswerDraft } from "./LiveAnswerDraft";
import { ResearchLiveScrollButton } from "./ResearchLiveScrollButton";

const AnswerContentRevisionContext = createContext<(() => void) | null>(null);
const ActiveRunSnapshotContext = createContext<ResearchRunLiveSnapshot | null>(
  null,
);

interface ResearchLiveScrollRegionProps {
  finalContentKey: string;
  children: ReactNode;
}

export function ResearchLiveScrollRegion({
  finalContentKey,
  children,
}: ResearchLiveScrollRegionProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const previousFinalContentKey = useRef(finalContentKey);
  const [contentRevision, setContentRevision] = useState(0);
  const markAnswerContentChanged = useCallback(
    () => setContentRevision((revision) => revision + 1),
    [],
  );

  useEffect(() => {
    if (previousFinalContentKey.current === finalContentKey) return;
    previousFinalContentKey.current = finalContentKey;
    markAnswerContentChanged();
  }, [finalContentKey, markAnswerContentChanged]);

  return (
    <AnswerContentRevisionContext.Provider value={markAnswerContentChanged}>
      <div className="relative min-h-0 min-w-0 flex-1">
        <div
          ref={containerRef}
          className="h-full min-h-0 min-w-0 overflow-y-auto overflow-x-hidden px-4 py-5"
        >
          {children}
        </div>
        <ResearchLiveScrollButton
          containerRef={containerRef}
          contentRevision={contentRevision}
        />
      </div>
    </AnswerContentRevisionContext.Provider>
  );
}

interface ResearchActiveRunBoundaryProps {
  runId: string;
  initialStatus: Extract<ResearchMessageRun["status"], "queued" | "running">;
  initialStage: ResearchMessageRun["progressStage"];
  children: ReactNode;
}

export function ResearchActiveRunBoundary({
  runId,
  initialStatus,
  initialStage,
  children,
}: ResearchActiveRunBoundaryProps) {
  const snapshot = useResearchRunLiveState({
    runId,
    initialStatus,
    initialStage,
  });
  const markAnswerContentChanged = useContext(AnswerContentRevisionContext);
  const answerPresentationKey = `${snapshot.liveState.draftMode}\0${snapshot.liveState.draftText}`;
  const previousAnswerPresentationKey = useRef(answerPresentationKey);

  useEffect(() => {
    if (previousAnswerPresentationKey.current === answerPresentationKey) {
      return;
    }
    previousAnswerPresentationKey.current = answerPresentationKey;
    markAnswerContentChanged?.();
  }, [answerPresentationKey, markAnswerContentChanged]);

  return (
    <ActiveRunSnapshotContext.Provider value={snapshot}>
      {children}
    </ActiveRunSnapshotContext.Provider>
  );
}

function useActiveRunSnapshot(): ResearchRunLiveSnapshot {
  const snapshot = useContext(ActiveRunSnapshotContext);
  if (snapshot === null) {
    throw new Error("Research active run context is missing");
  }
  return snapshot;
}

export function ResearchActiveRunStatus() {
  const snapshot = useActiveRunSnapshot();
  if (snapshot.runStatus !== "queued" && snapshot.runStatus !== "running") {
    return null;
  }
  return (
    <ActiveRunStatus
      status={snapshot.runStatus}
      stage={snapshot.liveState.progressStage}
      activity={snapshot.liveState.currentActivity}
    />
  );
}

export function ResearchActiveRunDraft() {
  const snapshot = useActiveRunSnapshot();
  const terminal = snapshot.liveState.terminal;
  return (
    <LiveAnswerDraft
      status={snapshot.runStatus}
      draftMode={snapshot.liveState.draftMode}
      draftText={snapshot.liveState.draftText}
      errorCode={terminal?.status === "failed" ? terminal.errorCode : null}
    />
  );
}
