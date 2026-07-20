import { describe, expect, it } from "vitest";
import { parseResearchLiveEvent } from "./events";

const UINT64_MAX = "18446744073709551615";

function parse(eventName: string, data: unknown, lastEventId = "1-0") {
  return parseResearchLiveEvent({
    eventName,
    data: typeof data === "string" ? data : JSON.stringify(data),
    lastEventId,
  });
}

function streamId(raw = "1-0", milliseconds = 1n, sequence = 0n) {
  return { raw, milliseconds, sequence };
}

describe("parseResearchLiveEvent", () => {
  describe("known event projection", () => {
    it.each([
      {
        eventName: "attempt.started",
        data: { attemptEpoch: 1, extra: "discarded" },
        expected: {
          type: "attempt.started",
          attemptEpoch: 1,
          streamId: streamId(),
        },
      },
      {
        eventName: "stage",
        data: {
          attemptEpoch: 2,
          stage: "retrieving",
          providerMetadata: { secret: true },
        },
        expected: {
          type: "stage",
          attemptEpoch: 2,
          stage: "retrieving",
          streamId: streamId(),
        },
      },
      {
        eventName: "activity",
        data: {
          attemptEpoch: 3,
          activity: {
            type: "internal_search.started",
            queryCount: 2,
            hidden: "discarded",
          },
          rawPayload: "discarded",
        },
        expected: {
          type: "activity",
          attemptEpoch: 3,
          activity: {
            type: "internal_search.started",
            queryCount: 2,
          },
          streamId: streamId(),
        },
      },
      {
        eventName: "answer.delta",
        data: {
          attemptEpoch: 4,
          generation: 2,
          text: "表示する本文",
          internalEvent: "discarded",
        },
        expected: {
          type: "answer.delta",
          attemptEpoch: 4,
          generation: 2,
          text: "表示する本文",
          streamId: streamId(),
        },
      },
      {
        eventName: "answer.reset",
        data: { attemptEpoch: 5, generation: 3, raw: "discarded" },
        expected: {
          type: "answer.reset",
          attemptEpoch: 5,
          generation: 3,
          streamId: streamId(),
        },
      },
      {
        eventName: "terminal",
        data: {
          attemptEpoch: 6,
          status: "completed",
          errorCode: "cancelled",
          answer: "discarded",
        },
        expected: {
          type: "terminal",
          attemptEpoch: 6,
          status: "completed",
          streamId: streamId(),
        },
      },
      {
        eventName: "terminal",
        data: {
          attemptEpoch: 7,
          status: "policy_blocked",
          internalReason: "provider_safety_filter",
        },
        expected: {
          type: "terminal",
          attemptEpoch: 7,
          status: "policy_blocked",
          streamId: streamId(),
        },
      },
    ])("projects $eventName without unknown fields", ({
      eventName,
      data,
      expected,
    }) => {
      expect(parse(eventName, data)).toEqual({
        kind: "event",
        event: expected,
      });
    });

    it("preserves answer text without duplicating the backend citation filter", () => {
      expect(
        parse("answer.delta", {
          attemptEpoch: 1,
          generation: 1,
          text: "本文 [[1]]",
        }),
      ).toEqual({
        kind: "event",
        event: {
          type: "answer.delta",
          attemptEpoch: 1,
          generation: 1,
          text: "本文 [[1]]",
          streamId: streamId(),
        },
      });
    });
  });

  describe("activity projection", () => {
    it.each([
      {
        activity: { type: "internal_search.started", queryCount: 3 },
      },
      {
        activity: { type: "internal_search.completed", hitCount: 8 },
      },
      {
        activity: {
          type: "external_search.queries_generated",
          taskIndex: 0,
          queries: ["NVIDIA AI", "半導体需要"],
        },
      },
      {
        activity: {
          type: "external_search.candidates_fetched",
          taskIndex: 1,
          candidateCount: 12,
        },
      },
      {
        activity: {
          type: "external_search.evidence_selected",
          taskIndex: 2,
          evidenceCount: 4,
        },
      },
      {
        activity: {
          type: "question.resolved",
          standaloneQuestion: "NVIDIAの発表は株価へどう影響する？",
        },
      },
    ])("accepts $activity.type with its camelCase fields", ({ activity }) => {
      expect(parse("activity", { attemptEpoch: 1, activity })).toEqual({
        kind: "event",
        event: {
          type: "activity",
          attemptEpoch: 1,
          activity,
          streamId: streamId(),
        },
      });
    });

    it.each([
      { type: "internal_search.started", queryCount: -1 },
      { type: "internal_search.completed", hitCount: 1.5 },
      {
        type: "external_search.queries_generated",
        taskIndex: 0,
        queries: [""],
      },
      {
        type: "external_search.candidates_fetched",
        taskIndex: -1,
        candidateCount: 1,
      },
      {
        type: "external_search.evidence_selected",
        taskIndex: 0,
        evidenceCount: "4",
      },
      { type: "question.resolved", standaloneQuestion: "" },
    ])("rejects invalid fields for $type", (activity) => {
      expect(parse("activity", { attemptEpoch: 1, activity })).toEqual({
        kind: "event-local-invalid",
        reason: "malformed_data",
      });
    });

    it("drops unknown activity without retaining its discriminator or payload", () => {
      const result = parse("activity", {
        attemptEpoch: 1,
        activity: { type: "private.activity", answerText: "sensitive" },
      });

      expect(result).toEqual({
        kind: "event-local-invalid",
        reason: "unknown_activity",
      });
      expect(JSON.stringify(result)).not.toContain("private.activity");
      expect(JSON.stringify(result)).not.toContain("sensitive");
    });
  });

  describe("event-local invalid data", () => {
    it("drops an unknown SSE event using a fixed reason", () => {
      const result = parse("private.event", {
        attemptEpoch: 1,
        answerText: "sensitive",
      });

      expect(result).toEqual({
        kind: "event-local-invalid",
        reason: "unknown_event",
      });
      expect(JSON.stringify(result)).not.toContain("private.event");
      expect(JSON.stringify(result)).not.toContain("sensitive");
    });

    it("drops malformed JSON", () => {
      expect(parse("stage", "{")).toEqual({
        kind: "event-local-invalid",
        reason: "malformed_data",
      });
    });

    it.each([
      null,
      [],
      JSON.stringify("text"),
      1,
      true,
    ])("drops non-object JSON: %j", (data) => {
      expect(parse("attempt.started", data)).toEqual({
        kind: "event-local-invalid",
        reason: "malformed_data",
      });
    });

    it.each([
      ["attempt.started", {}],
      ["stage", { attemptEpoch: 1, stage: "unknown" }],
      ["activity", { attemptEpoch: 1, activity: null }],
      ["answer.delta", { attemptEpoch: 1, generation: 1, text: "" }],
      ["answer.reset", { attemptEpoch: 1, generation: "1" }],
      ["terminal", { attemptEpoch: 1, status: 1 }],
    ])("drops invalid fields for %s", (eventName, data) => {
      expect(parse(eventName, data)).toEqual({
        kind: "event-local-invalid",
        reason: "malformed_data",
      });
    });
  });

  describe("positive safe integer fields", () => {
    it.each([
      0,
      -1,
      1.5,
      Number.MAX_SAFE_INTEGER + 1,
    ])("rejects attemptEpoch %s", (attemptEpoch) => {
      expect(parse("stage", { attemptEpoch, stage: "planning" })).toEqual({
        kind: "event-local-invalid",
        reason: "malformed_data",
      });
    });

    it.each([
      0,
      -1,
      1.5,
      Number.MAX_SAFE_INTEGER + 1,
    ])("rejects generation %s", (generation) => {
      expect(
        parse("answer.delta", {
          attemptEpoch: 1,
          generation,
          text: "本文",
        }),
      ).toEqual({
        kind: "event-local-invalid",
        reason: "malformed_data",
      });
    });

    it("accepts Number.MAX_SAFE_INTEGER at the inclusive boundary", () => {
      expect(
        parse("answer.delta", {
          attemptEpoch: Number.MAX_SAFE_INTEGER,
          generation: Number.MAX_SAFE_INTEGER,
          text: "本文",
        }),
      ).toEqual({
        kind: "event",
        event: {
          type: "answer.delta",
          attemptEpoch: Number.MAX_SAFE_INTEGER,
          generation: Number.MAX_SAFE_INTEGER,
          text: "本文",
          streamId: streamId(),
        },
      });
    });
  });

  describe("terminal normalization", () => {
    it("accepts policy_blocked without requiring or exposing errorCode", () => {
      const result = parse("terminal", {
        attemptEpoch: 1,
        status: "policy_blocked",
      });

      expect(result).toEqual({
        kind: "event",
        event: {
          type: "terminal",
          attemptEpoch: 1,
          status: "policy_blocked",
          streamId: streamId(),
        },
      });
      if (
        result.kind !== "event" ||
        result.event.type !== "terminal" ||
        result.event.status !== "policy_blocked"
      ) {
        throw new Error("policy_blocked terminal was not parsed");
      }
      expect(result.event).not.toHaveProperty("errorCode");
    });

    it("drops an unknown terminal status", () => {
      expect(
        parse("terminal", { attemptEpoch: 1, status: "cancelled" }),
      ).toEqual({
        kind: "event-local-invalid",
        reason: "malformed_data",
      });
    });

    it.each([
      undefined,
      "future_error_code",
    ])("normalizes failed errorCode %s to generic internal_error", (errorCode) => {
      expect(
        parse("terminal", { attemptEpoch: 1, status: "failed", errorCode }),
      ).toEqual({
        kind: "event",
        event: {
          type: "terminal",
          attemptEpoch: 1,
          status: "failed",
          errorCode: "internal_error",
          streamId: streamId(),
        },
      });
    });

    it("preserves a known failed errorCode", () => {
      expect(
        parse("terminal", {
          attemptEpoch: 1,
          status: "failed",
          errorCode: "cancelled",
        }),
      ).toEqual({
        kind: "event",
        event: {
          type: "terminal",
          attemptEpoch: 1,
          status: "failed",
          errorCode: "cancelled",
          streamId: streamId(),
        },
      });
    });

    it("drops errorCode from a completed terminal", () => {
      expect(
        parse("terminal", {
          attemptEpoch: 1,
          status: "completed",
          errorCode: "internal_error",
        }),
      ).toEqual({
        kind: "event",
        event: {
          type: "terminal",
          attemptEpoch: 1,
          status: "completed",
          streamId: streamId(),
        },
      });
    });
  });

  describe("Redis Stream ID integrity", () => {
    it.each([
      [undefined, "missing"],
      ["", "empty"],
      ["-1-0", "negative sign"],
      ["+1-0", "positive sign"],
      ["01-0", "leading zero in milliseconds"],
      ["0-01", "leading zero in sequence"],
      ["1-", "empty sequence"],
      ["-1", "empty milliseconds"],
      ["1-2-3", "additional separator"],
      ["1-a", "non-decimal"],
      [`${UINT64_MAX}0-${UINT64_MAX}`, "longer than 41 characters"],
      ["18446744073709551616-0", "milliseconds above uint64"],
      ["0-18446744073709551616", "sequence above uint64"],
    ])("classifies %s (%s) as a protocol integrity failure", (id, _description) => {
      expect(
        parseResearchLiveEvent({
          eventName: "attempt.started",
          data: JSON.stringify({ attemptEpoch: 1 }),
          lastEventId: id,
        }),
      ).toEqual({
        kind: "protocol-integrity-failure",
        reason: "invalid_stream_id",
      });
    });

    it.each([
      ["0-0", 0n, 0n],
      ["9-10", 9n, 10n],
      [
        `${UINT64_MAX}-${UINT64_MAX}`,
        18446744073709551615n,
        18446744073709551615n,
      ],
    ])("converts canonical ID %s to an exact BigInt pair", (raw, ms, seq) => {
      expect(parse("attempt.started", { attemptEpoch: 1 }, raw)).toEqual({
        kind: "event",
        event: {
          type: "attempt.started",
          attemptEpoch: 1,
          streamId: streamId(raw, ms, seq),
        },
      });
    });
  });
});
