import {
  parseResearchLiveActivity,
  parseResearchLiveEvent,
  type ResearchLiveActivity,
  type ResearchLiveErrorCode,
} from "./events";
import {
  createInitialResearchLiveState,
  mergeResearchLivePollProgress,
  type ResearchLiveState,
  type ResearchLiveTerminal,
  reduceResearchLiveEvent,
  suppressResearchLiveDraft,
} from "./reducer";

const PUBLIC_EVENT_NAMES = [
  "attempt.started",
  "stage",
  "activity",
  "answer.delta",
  "answer.reset",
  "terminal",
] as const;
const POLLING_SUCCESS_DELAY_MS = 2_000;
const MAX_RETRY_DELAY_MS = 10_000;
const EVENT_SOURCE_CLOSED = 2;

export type ResearchRunLiveConnectionMode =
  | "connecting"
  | "live"
  | "reconnecting"
  | "polling-only"
  | "finalizing"
  | "terminal";

export type ResearchRunLiveStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed";

export type ResearchRunLivePollResult =
  | {
      kind: "run";
      run: {
        status: ResearchRunLiveStatus;
        progressStage: ResearchLiveState["progressStage"];
        attemptEpoch: number | null;
        recentEvents: readonly unknown[];
        errorCode: ResearchLiveErrorCode | null;
      };
    }
  | { kind: "http-error"; status: 401 | 403 | 404 }
  | { kind: "unavailable" };

export interface ResearchRunLiveSnapshot {
  runStatus: ResearchRunLiveStatus;
  connectionMode: ResearchRunLiveConnectionMode;
  liveState: ResearchLiveState;
}

export interface ResearchRunLiveVisibility {
  isHidden: () => boolean;
  subscribe: (listener: () => void) => () => void;
}

export interface CreateResearchRunLiveControllerOptions {
  runId: string;
  initialStatus: "queued" | "running";
  initialStage: ResearchLiveState["progressStage"];
  pollRun?: (
    runId: string,
    signal: AbortSignal,
  ) => Promise<ResearchRunLivePollResult>;
  createEventSource?: (url: string) => EventSource;
  requestRefresh: () => Promise<void>;
  visibility?: ResearchRunLiveVisibility;
}

export interface ResearchRunLiveController {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => ResearchRunLiveSnapshot;
}

export function createResearchRunLiveController({
  runId,
  initialStatus,
  initialStage,
  pollRun = pollResearchRun,
  createEventSource = (url) => new EventSource(url),
  requestRefresh,
  visibility = browserVisibility,
}: CreateResearchRunLiveControllerOptions): ResearchRunLiveController {
  let snapshot: ResearchRunLiveSnapshot = {
    runStatus: initialStatus,
    connectionMode: "connecting",
    liveState: {
      ...createInitialResearchLiveState(),
      progressStage: initialStage,
    },
  };
  const listeners = new Set<() => void>();
  let lifecycleVersion = 0;
  let lifecycleActive = false;
  let eventSource: EventSource | null = null;
  let eventSourceListeners: Array<readonly [string, EventListener]> = [];
  let visibilityUnsubscribe: (() => void) | null = null;
  let pollingTimer: ReturnType<typeof setTimeout> | null = null;
  let finalizationTimer: ReturnType<typeof setTimeout> | null = null;
  let finalizationRequest: Promise<void> | null = null;
  let pollingRequest: AbortController | null = null;
  let pollingFailureCount = 0;
  let finalizationRetryIndex = 0;
  let finalizationStarted = false;
  let permanentlyStopped = false;

  const getSnapshot = () => snapshot;

  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    if (listeners.size === 1) startLifecycle();
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) stopLifecycle();
    };
  };

  function startLifecycle(): void {
    if (lifecycleActive || permanentlyStopped) return;
    lifecycleActive = true;
    lifecycleVersion += 1;
    const version = lifecycleVersion;
    visibilityUnsubscribe = visibility.subscribe(() =>
      handleVisibilityChange(version),
    );

    if (finalizationStarted) {
      startFinalizationRefresh(version);
      return;
    }

    if (snapshot.connectionMode !== "polling-only") {
      openEventSource(version);
    }
    startPoll(version);
  }

  function stopLifecycle(): void {
    if (!lifecycleActive) return;
    lifecycleActive = false;
    lifecycleVersion += 1;
    clearPollingTimer();
    clearFinalizationTimer();
    pollingRequest?.abort();
    pollingRequest = null;
    visibilityUnsubscribe?.();
    visibilityUnsubscribe = null;
    closeEventSource();
  }

  function openEventSource(version: number): void {
    if (!isCurrent(version) || eventSource !== null) return;
    const source = createEventSource(`/api/research/runs/${runId}/events`);
    eventSource = source;

    const openListener: EventListener = () => {
      if (!isCurrentSource(version, source) || finalizationStarted) return;
      updateSnapshot({ ...snapshot, connectionMode: "live" });
    };
    const errorListener: EventListener = () => {
      if (!isCurrentSource(version, source) || finalizationStarted) return;
      if (source.readyState === EVENT_SOURCE_CLOSED) {
        degradeToPollingOnly();
        return;
      }
      updateSnapshot({ ...snapshot, connectionMode: "reconnecting" });
    };

    addEventSourceListener(source, "open", openListener);
    addEventSourceListener(source, "error", errorListener);
    for (const eventName of PUBLIC_EVENT_NAMES) {
      const eventListener: EventListener = (event) => {
        if (
          !isCurrentSource(version, source) ||
          finalizationStarted ||
          !(event instanceof MessageEvent)
        ) {
          return;
        }
        const result = parseResearchLiveEvent({
          eventName,
          data: event.data,
          lastEventId: event.lastEventId,
        });
        if (result.kind === "event-local-invalid") return;
        if (result.kind === "protocol-integrity-failure") {
          degradeToPollingOnly();
          return;
        }

        const transition = reduceResearchLiveEvent(
          snapshot.liveState,
          result.event,
        );
        if (transition.state !== snapshot.liveState) {
          updateSnapshot({
            ...snapshot,
            runStatus:
              result.event.type !== "terminal" &&
              snapshot.runStatus === "queued"
                ? "running"
                : snapshot.runStatus,
            liveState: transition.state,
          });
        }
        if (transition.acceptedTerminal !== null) {
          beginFinalization(transition.acceptedTerminal, version);
        }
      };
      addEventSourceListener(source, eventName, eventListener);
    }
  }

  function addEventSourceListener(
    source: EventSource,
    type: string,
    listener: EventListener,
  ): void {
    source.addEventListener(type, listener);
    eventSourceListeners.push([type, listener]);
  }

  function closeEventSource(): void {
    if (eventSource === null) return;
    for (const [type, listener] of eventSourceListeners) {
      eventSource.removeEventListener(type, listener);
    }
    eventSourceListeners = [];
    eventSource.close();
    eventSource = null;
  }

  function degradeToPollingOnly(): void {
    if (finalizationStarted || snapshot.connectionMode === "polling-only") {
      return;
    }
    closeEventSource();
    updateSnapshot({
      ...snapshot,
      connectionMode: "polling-only",
      liveState: suppressResearchLiveDraft(snapshot.liveState),
    });
  }

  function startPoll(version: number): void {
    if (
      !isCurrent(version) ||
      finalizationStarted ||
      visibility.isHidden() ||
      pollingRequest !== null
    ) {
      return;
    }
    const request = new AbortController();
    pollingRequest = request;
    void pollRun(runId, request.signal)
      .then((result) => {
        if (!isCurrent(version) || request.signal.aborted) return;
        handlePollResult(result, version);
      })
      .catch(() => {
        if (!isCurrent(version) || request.signal.aborted) return;
        scheduleFailedPoll(version);
      })
      .finally(() => {
        if (pollingRequest === request) pollingRequest = null;
      });
  }

  function handlePollResult(
    result: ResearchRunLivePollResult,
    version: number,
  ): void {
    if (finalizationStarted) return;
    if (result.kind === "http-error") {
      stopPermanentlyForHttpError(version);
      return;
    }
    if (result.kind === "unavailable") {
      scheduleFailedPoll(version);
      return;
    }

    pollingFailureCount = 0;
    if (result.run.status === "completed" || result.run.status === "failed") {
      const terminal: ResearchLiveTerminal =
        result.run.status === "completed"
          ? { status: "completed" }
          : {
              status: "failed",
              errorCode: result.run.errorCode ?? "internal_error",
            };
      beginFinalization(terminal, version);
      return;
    }

    const runStatus = mergeActiveRunStatus(
      snapshot.runStatus,
      result.run.status,
    );
    let liveState = snapshot.liveState;
    if (isUsableAttemptEpoch(result.run.attemptEpoch)) {
      const progressMerge = mergeResearchLivePollProgress(
        liveState,
        result.run.attemptEpoch,
        result.run.progressStage,
      );
      liveState = progressMerge.state;
      if (
        (progressMerge.kind === "initial" &&
          shouldApplyInitialPollingActivity()) ||
        (progressMerge.kind === "equal" &&
          snapshot.connectionMode === "polling-only")
      ) {
        liveState = {
          ...liveState,
          currentActivity: latestRelevantPollingActivity(
            liveState.progressStage,
            result.run.recentEvents,
          ),
        };
      }
    }
    if (runStatus !== snapshot.runStatus || liveState !== snapshot.liveState) {
      updateSnapshot({
        ...snapshot,
        runStatus,
        liveState,
      });
    }
    schedulePoll(POLLING_SUCCESS_DELAY_MS, version);
  }

  function shouldApplyInitialPollingActivity(): boolean {
    return (
      snapshot.connectionMode === "polling-only" ||
      (snapshot.connectionMode === "connecting" &&
        !snapshot.liveState.hasAcceptedSseEvent)
    );
  }

  function scheduleFailedPoll(version: number): void {
    pollingFailureCount += 1;
    const delay = Math.min(
      POLLING_SUCCESS_DELAY_MS * 2 ** pollingFailureCount,
      MAX_RETRY_DELAY_MS,
    );
    schedulePoll(delay, version);
  }

  function schedulePoll(delay: number, version: number): void {
    clearPollingTimer();
    if (!isCurrent(version) || finalizationStarted || visibility.isHidden()) {
      return;
    }
    pollingTimer = setTimeout(() => {
      pollingTimer = null;
      startPoll(version);
    }, delay);
  }

  function beginFinalization(
    terminal: ResearchLiveTerminal,
    version: number,
  ): void {
    if (!isCurrent(version) || finalizationStarted) return;
    finalizationStarted = true;
    clearPollingTimer();
    pollingRequest?.abort();
    closeEventSource();
    const terminalLiveState =
      terminal.status === "failed"
        ? suppressResearchLiveDraft(snapshot.liveState)
        : snapshot.liveState;
    const liveState = { ...terminalLiveState, terminal };
    updateSnapshot({
      runStatus: terminal.status,
      connectionMode: "finalizing",
      liveState,
    });
    finalizationRetryIndex = 0;
    startFinalizationRefresh(version);
  }

  function stopPermanentlyForHttpError(version: number): void {
    if (!isCurrent(version) || permanentlyStopped) return;
    permanentlyStopped = true;
    lifecycleActive = false;
    lifecycleVersion += 1;
    clearPollingTimer();
    clearFinalizationTimer();
    pollingRequest?.abort();
    pollingRequest = null;
    visibilityUnsubscribe?.();
    visibilityUnsubscribe = null;
    closeEventSource();
    updateSnapshot({
      ...snapshot,
      connectionMode: "polling-only",
      liveState: suppressResearchLiveDraft(snapshot.liveState),
    });
    void requestRefresh().catch(() => undefined);
  }

  function startFinalizationRefresh(version: number): void {
    if (
      !isCurrent(version) ||
      !finalizationStarted ||
      visibility.isHidden() ||
      finalizationRequest !== null ||
      finalizationTimer !== null
    ) {
      return;
    }

    const request = requestRefresh();
    finalizationRequest = request;
    void request.then(
      () => settleFinalizationRefresh(request),
      () => discardFinalizationRefresh(request),
    );
  }

  function settleFinalizationRefresh(request: Promise<void>): void {
    if (finalizationRequest !== request) return;
    finalizationRequest = null;
    if (!lifecycleActive || !finalizationStarted) return;
    const delay = Math.min(
      POLLING_SUCCESS_DELAY_MS * 2 ** finalizationRetryIndex,
      MAX_RETRY_DELAY_MS,
    );
    finalizationRetryIndex += 1;
    if (visibility.isHidden()) return;
    const version = lifecycleVersion;
    finalizationTimer = setTimeout(() => {
      finalizationTimer = null;
      startFinalizationRefresh(version);
    }, delay);
  }

  function discardFinalizationRefresh(request: Promise<void>): void {
    if (finalizationRequest === request) finalizationRequest = null;
  }

  function handleVisibilityChange(version: number): void {
    if (!isCurrent(version)) return;
    if (visibility.isHidden()) {
      clearPollingTimer();
      clearFinalizationTimer();
      pollingRequest?.abort();
      pollingRequest = null;
      return;
    }
    if (finalizationStarted) {
      startFinalizationRefresh(version);
      return;
    }
    startPoll(version);
  }

  function clearPollingTimer(): void {
    if (pollingTimer === null) return;
    clearTimeout(pollingTimer);
    pollingTimer = null;
  }

  function clearFinalizationTimer(): void {
    if (finalizationTimer === null) return;
    clearTimeout(finalizationTimer);
    finalizationTimer = null;
  }

  function isCurrent(version: number): boolean {
    return lifecycleActive && lifecycleVersion === version;
  }

  function isCurrentSource(version: number, source: EventSource): boolean {
    return isCurrent(version) && eventSource === source;
  }

  function updateSnapshot(nextSnapshot: ResearchRunLiveSnapshot): void {
    if (nextSnapshot === snapshot) return;
    snapshot = nextSnapshot;
    for (const listener of listeners) listener();
  }

  return { subscribe, getSnapshot };
}

async function pollResearchRun(
  runId: string,
  signal: AbortSignal,
): Promise<ResearchRunLivePollResult> {
  try {
    const response = await fetch(`/api/research/runs/${runId}`, {
      cache: "no-store",
      signal,
    });
    if (
      response.status === 401 ||
      response.status === 403 ||
      response.status === 404
    ) {
      return { kind: "http-error", status: response.status };
    }
    if (!response.ok) return { kind: "unavailable" };

    const value: unknown = await response.json();
    const run = parsePollRun(value);
    return run === null ? { kind: "unavailable" } : { kind: "run", run };
  } catch {
    return { kind: "unavailable" };
  }
}

function parsePollRun(
  value: unknown,
): Extract<ResearchRunLivePollResult, { kind: "run" }>["run"] | null {
  if (!isRecord(value) || !isRunStatus(value.status)) return null;
  const progressStage = isProgressStage(value.progressStage)
    ? value.progressStage
    : null;
  const errorCode = isErrorCode(value.errorCode) ? value.errorCode : null;
  const recentEvents = Array.isArray(value.recentEvents)
    ? value.recentEvents.flatMap((event) => {
        const activity = parseResearchLiveActivity(event);
        return activity === null ? [] : [activity];
      })
    : [];
  const attemptEpoch = isUsableAttemptEpoch(value.attemptEpoch)
    ? value.attemptEpoch
    : null;
  return {
    status: value.status,
    progressStage,
    attemptEpoch,
    recentEvents,
    errorCode,
  };
}

function latestRelevantPollingActivity(
  progressStage: ResearchLiveState["progressStage"],
  recentEvents: readonly unknown[],
): ResearchLiveActivity | null {
  if (progressStage === "synthesizing") return null;
  for (let index = recentEvents.length - 1; index >= 0; index -= 1) {
    const activity = parseResearchLiveActivity(recentEvents[index]);
    if (activity === null) continue;
    if (
      (progressStage === null || progressStage === "planning") &&
      activity.type === "question.resolved"
    ) {
      return activity;
    }
    if (
      progressStage === "retrieving" &&
      activity.type !== "question.resolved"
    ) {
      return activity;
    }
  }
  return null;
}

function isRunStatus(value: unknown): value is ResearchRunLiveStatus {
  return (
    value === "queued" ||
    value === "running" ||
    value === "completed" ||
    value === "failed"
  );
}

function isProgressStage(
  value: unknown,
): value is NonNullable<ResearchLiveState["progressStage"]> {
  return (
    value === "planning" || value === "retrieving" || value === "synthesizing"
  );
}

function isErrorCode(
  value: unknown,
): value is Exclude<
  Extract<ResearchRunLivePollResult, { kind: "run" }>["run"]["errorCode"],
  null
> {
  return (
    value === "generation_unavailable" ||
    value === "internal_error" ||
    value === "enqueue_failed" ||
    value === "stale" ||
    value === "cancelled"
  );
}

function mergeActiveRunStatus(
  current: ResearchRunLiveStatus,
  incoming: "queued" | "running",
): ResearchRunLiveStatus {
  if (current === "completed" || current === "failed") return current;
  return current === "running" || incoming === "running" ? "running" : "queued";
}

function isUsableAttemptEpoch(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const browserVisibility: ResearchRunLiveVisibility = {
  isHidden: () => document.hidden,
  subscribe: (listener) => {
    document.addEventListener("visibilitychange", listener);
    return () => document.removeEventListener("visibilitychange", listener);
  },
};
