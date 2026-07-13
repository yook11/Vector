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
import { createInitialResearchLiveState } from "../live/reducer";
import { ActiveRunStatus, activeRunText } from "./ActiveRunStatus";
import { failureText, LiveAnswerSlotContent } from "./LiveAnswerDraft";
import { useResearchLiveAnnouncementReporter } from "./ResearchLiveAnnouncer";
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
  const [finalReplacementRevision, setFinalReplacementRevision] = useState(0);
  const markAnswerContentChanged = useCallback(
    () => setContentRevision((revision) => revision + 1),
    [],
  );

  useEffect(() => {
    if (previousFinalContentKey.current === finalContentKey) return;
    previousFinalContentKey.current = finalContentKey;
    setContentRevision((revision) => revision + 1);
    setFinalReplacementRevision((revision) => revision + 1);
  }, [finalContentKey]);

  return (
    <AnswerContentRevisionContext.Provider value={markAnswerContentChanged}>
      <div className="relative min-h-0 min-w-0 flex-1">
        <div
          ref={containerRef}
          className="h-full min-h-0 min-w-0 overflow-y-auto overflow-x-hidden overscroll-contain px-4 py-5"
        >
          {children}
        </div>
        <ResearchLiveScrollButton
          containerRef={containerRef}
          contentRevision={contentRevision}
          finalReplacementRevision={finalReplacementRevision}
        />
      </div>
    </AnswerContentRevisionContext.Provider>
  );
}

interface ResearchActiveRunBoundaryProps {
  runId: string;
  initialStatus: Extract<
    ResearchMessageRun["status"],
    "queued" | "running"
  > | null;
  initialStage: ResearchMessageRun["progressStage"];
  children: ReactNode;
}

interface ResearchActiveRunControllerProps {
  runId: string;
  initialStatus: Extract<ResearchMessageRun["status"], "queued" | "running">;
  initialStage: ResearchMessageRun["progressStage"];
  onSnapshot: (snapshot: ResearchRunLiveSnapshot) => void;
}

function ResearchActiveRunController({
  runId,
  initialStatus,
  initialStage,
  onSnapshot,
}: ResearchActiveRunControllerProps) {
  const snapshot = useResearchRunLiveState({
    runId,
    initialStatus,
    initialStage,
  });

  useEffect(() => {
    onSnapshot(snapshot);
  }, [onSnapshot, snapshot]);

  return null;
}

export function ResearchActiveRunBoundary({
  runId,
  initialStatus,
  initialStage,
  children,
}: ResearchActiveRunBoundaryProps) {
  const [snapshot, setSnapshot] = useState<ResearchRunLiveSnapshot | null>(
    () =>
      initialStatus === null
        ? null
        : {
            runStatus: initialStatus,
            connectionMode: "connecting",
            liveState: {
              ...createInitialResearchLiveState(),
              progressStage: initialStage,
            },
          },
  );
  const markAnswerContentChanged = useContext(AnswerContentRevisionContext);
  const reportLiveAnnouncement = useResearchLiveAnnouncementReporter();
  const answerPresentationKey =
    snapshot === null
      ? "inactive"
      : `${snapshot.liveState.draftMode}\0${snapshot.liveState.draftText}`;
  const previousAnswerPresentationKey = useRef(answerPresentationKey);
  const updateSnapshot = useCallback(
    (nextSnapshot: ResearchRunLiveSnapshot) => setSnapshot(nextSnapshot),
    [],
  );

  useEffect(() => {
    if (previousAnswerPresentationKey.current === answerPresentationKey) {
      return;
    }
    previousAnswerPresentationKey.current = answerPresentationKey;
    markAnswerContentChanged?.();
  }, [answerPresentationKey, markAnswerContentChanged]);

  useEffect(() => {
    if (snapshot === null) return;
    let announcement: string;
    if (snapshot.runStatus === "failed") {
      const terminal = snapshot.liveState.terminal;
      announcement = failureText(
        terminal?.status === "failed" ? terminal.errorCode : null,
      );
    } else if (snapshot.runStatus === "completed") {
      announcement = "回答を確定しています…";
    } else if (
      snapshot.liveState.draftMode === "visible" &&
      snapshot.liveState.draftText.length > 0
    ) {
      announcement = "回答を生成中…";
    } else {
      announcement = activeRunText(
        snapshot.runStatus,
        snapshot.liveState.progressStage,
      );
    }
    reportLiveAnnouncement?.(runId, announcement);
  }, [reportLiveAnnouncement, runId, snapshot]);

  return (
    <>
      {initialStatus === null ? null : (
        <ResearchActiveRunController
          key="live-controller"
          runId={runId}
          initialStatus={initialStatus}
          initialStage={initialStage}
          onSnapshot={updateSnapshot}
        />
      )}
      <ActiveRunSnapshotContext.Provider key="presentation" value={snapshot}>
        {children}
      </ActiveRunSnapshotContext.Provider>
    </>
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

export function ResearchActiveRunAnswerContent() {
  const snapshot = useActiveRunSnapshot();
  const terminal = snapshot.liveState.terminal;
  return (
    <LiveAnswerSlotContent
      status={snapshot.runStatus}
      draftMode={snapshot.liveState.draftMode}
      draftText={snapshot.liveState.draftText}
      errorCode={terminal?.status === "failed" ? terminal.errorCode : null}
    />
  );
}
