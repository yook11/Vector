const REDIS_STREAM_ID_PATTERN = /^(0|[1-9][0-9]*)-(0|[1-9][0-9]*)$/;
const REDIS_STREAM_ID_MAX_LENGTH = 41;
const UINT64_MAX = 18_446_744_073_709_551_615n;

const STAGES = ["planning", "retrieving", "synthesizing"] as const;
const ERROR_CODES = [
  "generation_unavailable",
  "internal_error",
  "enqueue_failed",
  "stale",
  "cancelled",
] as const;

export interface ResearchLiveStreamId {
  raw: string;
  milliseconds: bigint;
  sequence: bigint;
}

export type ResearchLiveStage = (typeof STAGES)[number];
export type ResearchLiveErrorCode = (typeof ERROR_CODES)[number];

export type ResearchLiveActivity =
  | {
      type: "internal_search.started";
      queryCount: number;
    }
  | {
      type: "internal_search.completed";
      hitCount: number;
    }
  | {
      type: "external_search.queries_generated";
      taskIndex: number;
      queries: string[];
    }
  | {
      type: "external_search.candidates_fetched";
      taskIndex: number;
      candidateCount: number;
    }
  | {
      type: "external_search.evidence_selected";
      taskIndex: number;
      evidenceCount: number;
    }
  | {
      type: "question.resolved";
      standaloneQuestion: string;
    };

interface ResearchLiveEventBase {
  attemptEpoch: number;
  streamId: ResearchLiveStreamId;
}

export type ResearchLiveEvent =
  | (ResearchLiveEventBase & {
      type: "attempt.started";
    })
  | (ResearchLiveEventBase & {
      type: "stage";
      stage: ResearchLiveStage;
    })
  | (ResearchLiveEventBase & {
      type: "activity";
      activity: ResearchLiveActivity;
    })
  | (ResearchLiveEventBase & {
      type: "answer.delta";
      generation: number;
      text: string;
    })
  | (ResearchLiveEventBase & {
      type: "answer.reset";
      generation: number;
    })
  | (ResearchLiveEventBase & {
      type: "terminal";
      status: "completed";
    })
  | (ResearchLiveEventBase & {
      type: "terminal";
      status: "policy_blocked";
    })
  | (ResearchLiveEventBase & {
      type: "terminal";
      status: "failed";
      errorCode: ResearchLiveErrorCode;
    });

export type ResearchLiveEventParseResult =
  | {
      kind: "event";
      event: ResearchLiveEvent;
    }
  | {
      kind: "event-local-invalid";
      reason: "unknown_event" | "malformed_data" | "unknown_activity";
    }
  | {
      kind: "protocol-integrity-failure";
      reason: "invalid_stream_id";
    };

interface ParseResearchLiveEventInput {
  eventName: string;
  data: string;
  lastEventId?: string;
}

export function parseResearchLiveEvent({
  eventName,
  data,
  lastEventId,
}: ParseResearchLiveEventInput): ResearchLiveEventParseResult {
  const streamId = parseStreamId(lastEventId);
  if (streamId === null) {
    return {
      kind: "protocol-integrity-failure",
      reason: "invalid_stream_id",
    };
  }

  if (!isKnownEventName(eventName)) {
    return { kind: "event-local-invalid", reason: "unknown_event" };
  }

  const payload = parseObject(data);
  if (payload === null || !isPositiveSafeInteger(payload.attemptEpoch)) {
    return { kind: "event-local-invalid", reason: "malformed_data" };
  }
  const attemptEpoch = payload.attemptEpoch;

  switch (eventName) {
    case "attempt.started":
      return {
        kind: "event",
        event: { type: "attempt.started", attemptEpoch, streamId },
      };
    case "stage": {
      if (!isStage(payload.stage)) return malformedData();
      return {
        kind: "event",
        event: { type: "stage", attemptEpoch, stage: payload.stage, streamId },
      };
    }
    case "activity": {
      const activity = parseActivity(payload.activity);
      if (activity === "unknown") {
        return { kind: "event-local-invalid", reason: "unknown_activity" };
      }
      if (activity === null) return malformedData();
      return {
        kind: "event",
        event: { type: "activity", attemptEpoch, activity, streamId },
      };
    }
    case "answer.delta":
      if (
        !isPositiveSafeInteger(payload.generation) ||
        typeof payload.text !== "string" ||
        payload.text.length === 0
      ) {
        return malformedData();
      }
      return {
        kind: "event",
        event: {
          type: "answer.delta",
          attemptEpoch,
          generation: payload.generation,
          text: payload.text,
          streamId,
        },
      };
    case "answer.reset":
      if (!isPositiveSafeInteger(payload.generation)) return malformedData();
      return {
        kind: "event",
        event: {
          type: "answer.reset",
          attemptEpoch,
          generation: payload.generation,
          streamId,
        },
      };
    case "terminal":
      if (payload.status === "completed") {
        return {
          kind: "event",
          event: {
            type: "terminal",
            attemptEpoch,
            status: "completed",
            streamId,
          },
        };
      }
      if (payload.status === "policy_blocked") {
        return {
          kind: "event",
          event: {
            type: "terminal",
            attemptEpoch,
            status: "policy_blocked",
            streamId,
          },
        };
      }
      if (payload.status !== "failed") return malformedData();
      return {
        kind: "event",
        event: {
          type: "terminal",
          attemptEpoch,
          status: "failed",
          errorCode: isErrorCode(payload.errorCode)
            ? payload.errorCode
            : "internal_error",
          streamId,
        },
      };
  }
}

export function parseResearchLiveActivity(
  value: unknown,
): ResearchLiveActivity | null {
  const activity = parseActivity(value);
  return activity === "unknown" ? null : activity;
}

function parseStreamId(value: string | undefined): ResearchLiveStreamId | null {
  if (
    value === undefined ||
    value.length > REDIS_STREAM_ID_MAX_LENGTH ||
    !REDIS_STREAM_ID_PATTERN.test(value)
  ) {
    return null;
  }
  const [millisecondsText, sequenceText] = value.split("-");
  if (millisecondsText === undefined || sequenceText === undefined) return null;

  const milliseconds = BigInt(millisecondsText);
  const sequence = BigInt(sequenceText);
  if (milliseconds > UINT64_MAX || sequence > UINT64_MAX) return null;

  return { raw: value, milliseconds, sequence };
}

function parseObject(data: string): Record<string, unknown> | null {
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    return null;
  }
  return isRecord(value) ? value : null;
}

function parseActivity(
  value: unknown,
): ResearchLiveActivity | "unknown" | null {
  if (!isRecord(value) || typeof value.type !== "string") return null;

  switch (value.type) {
    case "internal_search.started":
      return isNonNegativeSafeInteger(value.queryCount)
        ? { type: value.type, queryCount: value.queryCount }
        : null;
    case "internal_search.completed":
      return isNonNegativeSafeInteger(value.hitCount)
        ? { type: value.type, hitCount: value.hitCount }
        : null;
    case "external_search.queries_generated":
      return isNonNegativeSafeInteger(value.taskIndex) &&
        isNonBlankStringArray(value.queries)
        ? {
            type: value.type,
            taskIndex: value.taskIndex,
            queries: [...value.queries],
          }
        : null;
    case "external_search.candidates_fetched":
      return isNonNegativeSafeInteger(value.taskIndex) &&
        isNonNegativeSafeInteger(value.candidateCount)
        ? {
            type: value.type,
            taskIndex: value.taskIndex,
            candidateCount: value.candidateCount,
          }
        : null;
    case "external_search.evidence_selected":
      return isNonNegativeSafeInteger(value.taskIndex) &&
        isNonNegativeSafeInteger(value.evidenceCount)
        ? {
            type: value.type,
            taskIndex: value.taskIndex,
            evidenceCount: value.evidenceCount,
          }
        : null;
    case "question.resolved":
      return isNonBlankString(value.standaloneQuestion) &&
        value.standaloneQuestion.length <= 500
        ? { type: value.type, standaloneQuestion: value.standaloneQuestion }
        : null;
    default:
      return "unknown";
  }
}

function malformedData(): ResearchLiveEventParseResult {
  return { kind: "event-local-invalid", reason: "malformed_data" };
}

function isKnownEventName(value: string): value is ResearchLiveEvent["type"] {
  return (
    value === "attempt.started" ||
    value === "stage" ||
    value === "activity" ||
    value === "answer.delta" ||
    value === "answer.reset" ||
    value === "terminal"
  );
}

function isStage(value: unknown): value is ResearchLiveStage {
  return typeof value === "string" && STAGES.some((stage) => stage === value);
}

function isErrorCode(value: unknown): value is ResearchLiveErrorCode {
  return (
    typeof value === "string" && ERROR_CODES.some((code) => code === value)
  );
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isNonBlankString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isNonBlankStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isNonBlankString);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
