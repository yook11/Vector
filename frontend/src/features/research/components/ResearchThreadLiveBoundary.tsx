"use client";

import { AlertTriangle } from "lucide-react";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type {
  ResearchAssistantMessage,
  ResearchMessageRun,
} from "@/types/types.gen";
import { useResearchRunLiveState } from "../hooks/useResearchRunLiveState";
import type { ResearchRunLiveSnapshot } from "../live/controller";
import { createInitialResearchLiveState } from "../live/reducer";
import { ActiveRunStatus, activeRunText } from "./ActiveRunStatus";
import { failureText, LiveAnswerSlotContent } from "./LiveAnswerDraft";
import { ResearchAnswerSlot } from "./ResearchAnswerSlot";
import { useResearchLiveAnnouncementReporter } from "./ResearchLiveAnnouncer";
import { ResearchLiveScrollButton } from "./ResearchLiveScrollButton";

const AnswerContentRevisionContext = createContext<(() => void) | null>(null);
const FailedContractionRevisionContext = createContext<(() => void) | null>(
  null,
);
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
  const [failedContractionRevision, setFailedContractionRevision] = useState(0);
  const markAnswerContentChanged = useCallback(
    () => setContentRevision((revision) => revision + 1),
    [],
  );
  const markFailedContraction = useCallback(
    () => setFailedContractionRevision((revision) => revision + 1),
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
      <FailedContractionRevisionContext.Provider value={markFailedContraction}>
        <div className="relative min-h-0 min-w-0 flex-1">
          <div
            ref={containerRef}
            data-research-answer-scroll-region
            className="h-full min-h-0 min-w-0 overflow-y-auto overflow-x-hidden overscroll-contain px-4 py-5"
          >
            {children}
          </div>
          <ResearchLiveScrollButton
            containerRef={containerRef}
            contentRevision={contentRevision}
            finalReplacementRevision={finalReplacementRevision}
            failedContractionRevision={failedContractionRevision}
          />
        </div>
      </FailedContractionRevisionContext.Provider>
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
  const isLiveFailed = snapshot?.runStatus === "failed";
  const updateSnapshot = useCallback(
    (nextSnapshot: ResearchRunLiveSnapshot) => setSnapshot(nextSnapshot),
    [],
  );

  useEffect(() => {
    if (previousAnswerPresentationKey.current === answerPresentationKey) {
      return;
    }
    previousAnswerPresentationKey.current = answerPresentationKey;
    if (isLiveFailed) return;
    markAnswerContentChanged?.();
  }, [answerPresentationKey, isLiveFailed, markAnswerContentChanged]);

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

interface ResearchRunPresentationProps {
  run: ResearchMessageRun;
  isActive: boolean;
}

export function ResearchRunStatusRail({
  run,
  isActive,
}: ResearchRunPresentationProps) {
  const snapshot = useContext(ActiveRunSnapshotContext);
  const markFailedContraction = useContext(FailedContractionRevisionContext);
  const liveFailure =
    snapshot?.runStatus === "failed" &&
    snapshot.liveState.terminal?.status === "failed"
      ? snapshot.liveState.terminal
      : null;
  const isFailed = run.status === "failed" || liveFailure !== null;
  const previousIsFailed = useRef(isFailed);

  useLayoutEffect(() => {
    if (!previousIsFailed.current && isFailed) {
      markFailedContraction?.();
    }
    previousIsFailed.current = isFailed;
  }, [isFailed, markFailedContraction]);

  if (isFailed) {
    return (
      <div
        data-research-failure-rail
        className="mt-2 flex min-w-0 items-center gap-1.5 text-xs text-[var(--vector-ink-muted)]"
      >
        <AlertTriangle aria-hidden="true" className="size-3.5 shrink-0" />
        <span className="min-w-0 break-words [overflow-wrap:anywhere]">
          {failureText(liveFailure?.errorCode ?? run.errorCode)}
        </span>
      </div>
    );
  }
  if (
    !isActive ||
    snapshot === null ||
    (snapshot.runStatus !== "queued" && snapshot.runStatus !== "running")
  ) {
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

interface ResearchRunAnswerSlotProps extends ResearchRunPresentationProps {
  finalAnswer: ResearchAssistantMessage | null;
}

export function ResearchRunAnswerSlot({
  run,
  isActive,
  finalAnswer,
}: ResearchRunAnswerSlotProps) {
  const snapshot = useContext(ActiveRunSnapshotContext);
  const isFailed = run.status === "failed" || snapshot?.runStatus === "failed";
  if (isFailed) return null;

  if (finalAnswer !== null) {
    return <ResearchAnswerSlot finalAnswer={finalAnswer} />;
  }
  if (!isActive || snapshot === null) return null;

  const terminal = snapshot.liveState.terminal;
  return (
    <ResearchAnswerSlot finalAnswer={null}>
      <LiveAnswerSlotContent
        status={snapshot.runStatus}
        draftMode={snapshot.liveState.draftMode}
        draftText={snapshot.liveState.draftText}
        errorCode={terminal?.status === "failed" ? terminal.errorCode : null}
      />
    </ResearchAnswerSlot>
  );
}
