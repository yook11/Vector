import type {
  ResearchLiveActivity,
  ResearchLiveErrorCode,
  ResearchLiveEvent,
  ResearchLiveStage,
  ResearchLiveStreamId,
} from "./events";

export type ResearchLiveDraftMode = "empty" | "visible" | "suppressed";

export type ResearchLiveTerminal =
  | { status: "completed" }
  | { status: "failed"; errorCode: ResearchLiveErrorCode };

export interface ResearchLiveState {
  currentAttemptEpoch: number | null;
  currentGeneration: number | null;
  progressStage: ResearchLiveStage | null;
  currentActivity: ResearchLiveActivity | null;
  draftText: string;
  draftMode: ResearchLiveDraftMode;
  lastProcessedEventId: ResearchLiveStreamId | null;
  hasAcceptedSseEvent: boolean;
  terminal: ResearchLiveTerminal | null;
}

export interface ResearchLiveTransition {
  state: ResearchLiveState;
  acceptedTerminal: ResearchLiveTerminal | null;
}

export type ResearchLivePollProgressMergeKind =
  | "initial"
  | "equal"
  | "higher"
  | "lower";

export interface ResearchLivePollProgressMerge {
  state: ResearchLiveState;
  kind: ResearchLivePollProgressMergeKind;
}

export function createInitialResearchLiveState(): ResearchLiveState {
  return {
    currentAttemptEpoch: null,
    currentGeneration: null,
    progressStage: null,
    currentActivity: null,
    draftText: "",
    draftMode: "empty",
    lastProcessedEventId: null,
    hasAcceptedSseEvent: false,
    terminal: null,
  };
}

export function reduceResearchLiveEvent(
  state: ResearchLiveState,
  event: ResearchLiveEvent,
): ResearchLiveTransition {
  if (state.terminal !== null || isAlreadyProcessed(state, event.streamId)) {
    return unchanged(state);
  }

  let nextState: ResearchLiveState = {
    ...state,
    lastProcessedEventId: event.streamId,
    hasAcceptedSseEvent: true,
  };

  if (
    nextState.currentAttemptEpoch === null ||
    event.attemptEpoch > nextState.currentAttemptEpoch
  ) {
    nextState = resetForAttempt(nextState, event.attemptEpoch);
  } else if (event.attemptEpoch < nextState.currentAttemptEpoch) {
    return unchanged(nextState);
  }

  switch (event.type) {
    case "attempt.started":
      return unchanged(nextState);
    case "stage":
      return unchanged({
        ...nextState,
        progressStage: advanceResearchLiveStage(
          nextState.progressStage,
          event.stage,
        ),
      });
    case "activity":
      return unchanged({
        ...nextState,
        currentActivity: event.activity,
      });
    case "answer.delta":
      return unchanged(applyDelta(nextState, event.generation, event.text));
    case "answer.reset":
      return unchanged(applyReset(nextState, event.generation));
    case "terminal": {
      const terminal: ResearchLiveTerminal =
        event.status === "completed"
          ? { status: "completed" }
          : { status: "failed", errorCode: event.errorCode };
      const terminalState =
        event.status === "completed"
          ? { ...nextState, terminal }
          : {
              ...nextState,
              draftText: "",
              draftMode: "suppressed" as const,
              terminal,
            };
      return { state: terminalState, acceptedTerminal: terminal };
    }
  }
}

export function mergeResearchLivePollProgress(
  state: ResearchLiveState,
  attemptEpoch: number,
  progressStage: ResearchLiveState["progressStage"],
): ResearchLivePollProgressMerge {
  if (state.currentAttemptEpoch === null) {
    return {
      state: applyProgressStage(
        resetForAttempt(state, attemptEpoch),
        progressStage,
      ),
      kind: "initial",
    };
  }
  if (attemptEpoch < state.currentAttemptEpoch) {
    return { state, kind: "lower" };
  }
  if (attemptEpoch > state.currentAttemptEpoch) {
    return {
      state: applyProgressStage(
        resetForAttempt(state, attemptEpoch),
        progressStage,
      ),
      kind: "higher",
    };
  }
  return { state: applyProgressStage(state, progressStage), kind: "equal" };
}

export function advanceResearchLiveStage(
  current: ResearchLiveState["progressStage"],
  next: ResearchLiveState["progressStage"],
): ResearchLiveState["progressStage"] {
  return stageRank(next) > stageRank(current) ? next : current;
}

export function suppressResearchLiveDraft(
  state: ResearchLiveState,
): ResearchLiveState {
  if (state.draftMode === "suppressed" && state.draftText.length === 0) {
    return state;
  }
  return { ...state, draftText: "", draftMode: "suppressed" };
}

function resetForAttempt(
  state: ResearchLiveState,
  attemptEpoch: number,
): ResearchLiveState {
  return {
    currentAttemptEpoch: attemptEpoch,
    currentGeneration: null,
    progressStage: null,
    currentActivity: null,
    draftText: "",
    draftMode: "empty",
    lastProcessedEventId: state.lastProcessedEventId,
    hasAcceptedSseEvent: state.hasAcceptedSseEvent,
    terminal: null,
  };
}

function applyProgressStage(
  state: ResearchLiveState,
  progressStage: ResearchLiveState["progressStage"],
): ResearchLiveState {
  const nextProgressStage = advanceResearchLiveStage(
    state.progressStage,
    progressStage,
  );
  return nextProgressStage === state.progressStage
    ? state
    : { ...state, progressStage: nextProgressStage };
}

function stageRank(stage: ResearchLiveState["progressStage"]): number {
  switch (stage) {
    case "planning":
      return 1;
    case "retrieving":
      return 2;
    case "synthesizing":
      return 3;
    case null:
      return 0;
  }
}

function applyDelta(
  state: ResearchLiveState,
  generation: number,
  text: string,
): ResearchLiveState {
  if (
    state.currentGeneration !== null &&
    generation < state.currentGeneration
  ) {
    return state;
  }
  if (state.draftMode === "suppressed") {
    return {
      ...state,
      currentGeneration: Math.max(
        state.currentGeneration ?? generation,
        generation,
      ),
    };
  }
  if (state.currentGeneration === generation) {
    return {
      ...state,
      draftText: `${state.draftText}${text}`,
      draftMode: "visible",
    };
  }
  return {
    ...state,
    currentGeneration: generation,
    draftText: text,
    draftMode: "visible",
  };
}

function applyReset(
  state: ResearchLiveState,
  generation: number,
): ResearchLiveState {
  if (
    state.currentGeneration !== null &&
    generation <= state.currentGeneration
  ) {
    return state;
  }
  return {
    ...state,
    currentGeneration: generation,
    draftText: "",
    draftMode: state.draftMode === "suppressed" ? "suppressed" : "empty",
  };
}

function isAlreadyProcessed(
  state: ResearchLiveState,
  streamId: ResearchLiveStreamId,
): boolean {
  return (
    state.lastProcessedEventId !== null &&
    compareStreamIds(streamId, state.lastProcessedEventId) <= 0
  );
}

function compareStreamIds(
  left: ResearchLiveStreamId,
  right: ResearchLiveStreamId,
): number {
  if (left.milliseconds < right.milliseconds) return -1;
  if (left.milliseconds > right.milliseconds) return 1;
  if (left.sequence < right.sequence) return -1;
  if (left.sequence > right.sequence) return 1;
  return 0;
}

function unchanged(state: ResearchLiveState): ResearchLiveTransition {
  return { state, acceptedTerminal: null };
}
