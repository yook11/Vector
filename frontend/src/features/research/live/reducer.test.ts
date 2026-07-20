import { describe, expect, it } from "vitest";
import type {
  ResearchLiveActivity,
  ResearchLiveEvent,
  ResearchLiveStreamId,
} from "./events";
import {
  createInitialResearchLiveState,
  reduceResearchLiveEvent,
  suppressResearchLiveDraft,
} from "./reducer";

type ResearchLiveTerminalEvent = Extract<
  ResearchLiveEvent,
  { type: "terminal" }
>;
type ResearchLiveStateHasActivityHistory =
  "activityHistory" extends keyof ReturnType<
    typeof createInitialResearchLiveState
  >
    ? true
    : false;
const STATE_HAS_ACTIVITY_HISTORY: ResearchLiveStateHasActivityHistory = false;

const RESOLVED_ACTIVITY: ResearchLiveActivity = {
  type: "question.resolved",
  standaloneQuestion: "旧attemptの質問",
};
const NEW_ACTIVITY: ResearchLiveActivity = {
  type: "internal_search.started",
  queryCount: 2,
};

function streamId(raw: string): ResearchLiveStreamId {
  const [milliseconds = "", sequence = ""] = raw.split("-");
  return {
    raw,
    milliseconds: BigInt(milliseconds),
    sequence: BigInt(sequence),
  };
}

function marker(raw: string, attemptEpoch: number): ResearchLiveEvent {
  return {
    type: "attempt.started",
    attemptEpoch,
    streamId: streamId(raw),
  };
}

function stage(
  raw: string,
  attemptEpoch: number,
  value: "planning" | "retrieving" | "synthesizing",
): ResearchLiveEvent {
  return {
    type: "stage",
    attemptEpoch,
    stage: value,
    streamId: streamId(raw),
  };
}

function activity(
  raw: string,
  attemptEpoch: number,
  value: ResearchLiveActivity,
): ResearchLiveEvent {
  return {
    type: "activity",
    attemptEpoch,
    activity: value,
    streamId: streamId(raw),
  };
}

function delta(
  raw: string,
  attemptEpoch: number,
  generation: number,
  text: string,
): ResearchLiveEvent {
  return {
    type: "answer.delta",
    attemptEpoch,
    generation,
    text,
    streamId: streamId(raw),
  };
}

function reset(
  raw: string,
  attemptEpoch: number,
  generation: number,
): ResearchLiveEvent {
  return {
    type: "answer.reset",
    attemptEpoch,
    generation,
    streamId: streamId(raw),
  };
}

function completed(
  raw: string,
  attemptEpoch: number,
): ResearchLiveTerminalEvent {
  return {
    type: "terminal",
    attemptEpoch,
    status: "completed",
    streamId: streamId(raw),
  };
}

function failed(raw: string, attemptEpoch: number): ResearchLiveTerminalEvent {
  return {
    type: "terminal",
    attemptEpoch,
    status: "failed",
    errorCode: "internal_error",
    streamId: streamId(raw),
  };
}

function policyBlocked(
  raw: string,
  attemptEpoch: number,
): ResearchLiveTerminalEvent {
  return {
    type: "terminal",
    attemptEpoch,
    status: "policy_blocked",
    streamId: streamId(raw),
  } as unknown as ResearchLiveTerminalEvent;
}

function apply(
  state: ReturnType<typeof createInitialResearchLiveState>,
  event: ResearchLiveEvent,
) {
  return reduceResearchLiveEvent(state, event).state;
}

function populatedAttemptOne() {
  let state = createInitialResearchLiveState();
  state = apply(state, marker("1-0", 1));
  state = apply(state, stage("2-0", 1, "synthesizing"));
  state = apply(state, activity("3-0", 1, RESOLVED_ACTIVITY));
  state = apply(state, delta("4-0", 1, 1, "古い回答"));
  return state;
}

function populatedAttemptTwo() {
  return apply(populatedAttemptOne(), delta("5-0", 2, 2, "現在の回答"));
}

describe("research live reducer", () => {
  it("keeps only the current activity and does not expose activity history", () => {
    let state = createInitialResearchLiveState();
    state = apply(state, activity("1-0", 1, RESOLVED_ACTIVITY));
    state = apply(state, activity("2-0", 1, NEW_ACTIVITY));

    expect(STATE_HAS_ACTIVITY_HISTORY).toBe(false);
    expect(state.currentActivity).toEqual(NEW_ACTIVITY);
    expect(state).not.toHaveProperty("activityHistory");
  });

  describe("attempt epoch fencing", () => {
    it.each([
      marker("1-0", 3),
      stage("1-0", 3, "planning"),
      activity("1-0", 3, NEW_ACTIVITY),
      delta("1-0", 3, 2, "suffix"),
      reset("1-0", 3, 2),
      completed("1-0", 3),
    ])("pins the first event epoch for $type", (event) => {
      const transition = reduceResearchLiveEvent(
        createInitialResearchLiveState(),
        event,
      );

      expect(transition.state.currentAttemptEpoch).toBe(3);
      expect(transition.state.lastProcessedEventId).toEqual(streamId("1-0"));
      expect(transition.state.hasAcceptedSseEvent).toBe(true);
    });

    it("resets every attempt-local field before applying a higher-epoch marker", () => {
      const transition = reduceResearchLiveEvent(
        populatedAttemptOne(),
        marker("5-0", 2),
      );

      expect(transition).toEqual({
        state: {
          currentAttemptEpoch: 2,
          currentGeneration: null,
          progressStage: null,
          currentActivity: null,
          draftText: "",
          draftMode: "empty",
          lastProcessedEventId: streamId("5-0"),
          hasAcceptedSseEvent: true,
          terminal: null,
        },
        acceptedTerminal: null,
      });
    });

    it.each([
      {
        event: stage("5-0", 2, "planning"),
        expected: {
          progressStage: "planning",
          currentActivity: null,
          currentGeneration: null,
          draftText: "",
          draftMode: "empty",
          terminal: null,
        },
      },
      {
        event: activity("5-0", 2, NEW_ACTIVITY),
        expected: {
          progressStage: null,
          currentActivity: NEW_ACTIVITY,
          currentGeneration: null,
          draftText: "",
          draftMode: "empty",
          terminal: null,
        },
      },
      {
        event: delta("5-0", 2, 4, "新しい回答"),
        expected: {
          progressStage: null,
          currentActivity: null,
          currentGeneration: 4,
          draftText: "新しい回答",
          draftMode: "visible",
          terminal: null,
        },
      },
      {
        event: completed("5-0", 2),
        expected: {
          progressStage: null,
          currentActivity: null,
          currentGeneration: null,
          draftText: "",
          draftMode: "empty",
          terminal: { status: "completed" },
        },
      },
    ])("clears old attempt state before a higher-epoch $event.type", ({
      event,
      expected,
    }) => {
      const transition = reduceResearchLiveEvent(populatedAttemptOne(), event);

      expect(transition.state).toMatchObject({
        currentAttemptEpoch: 2,
        lastProcessedEventId: streamId("5-0"),
        ...expected,
      });
      expect(transition.state).not.toHaveProperty("activityHistory");
    });

    it("ignores a lower epoch for display but consumes its newer Stream ID", () => {
      const current = populatedAttemptTwo();
      const transition = reduceResearchLiveEvent(
        current,
        stage("6-0", 1, "planning"),
      );

      expect(transition.state).toEqual({
        ...current,
        lastProcessedEventId: streamId("6-0"),
        hasAcceptedSseEvent: true,
      });
      expect(transition.acceptedTerminal).toBeNull();
    });

    it("keeps the current draft and progress for a duplicate marker", () => {
      const current = populatedAttemptOne();
      const transition = reduceResearchLiveEvent(current, marker("5-0", 1));

      expect(transition.state).toEqual({
        ...current,
        lastProcessedEventId: streamId("5-0"),
      });
      expect(transition.acceptedTerminal).toBeNull();
    });
  });

  describe("same-attempt stage monotonicity", () => {
    it("keeps synthesizing after delayed lower stages while accepting a direct skip", () => {
      let state = createInitialResearchLiveState();
      state = apply(state, marker("1-0", 1));
      state = apply(state, stage("2-0", 1, "planning"));
      state = apply(state, stage("3-0", 1, "synthesizing"));
      state = apply(state, stage("4-0", 1, "retrieving"));

      expect(state).toMatchObject({
        progressStage: "synthesizing",
        lastProcessedEventId: streamId("4-0"),
      });

      const skipped = apply(
        apply(createInitialResearchLiveState(), marker("1-0", 2)),
        stage("2-0", 2, "synthesizing"),
      );
      expect(skipped.progressStage).toBe("synthesizing");
    });
  });

  describe("generation boundaries", () => {
    it("clears the old draft on an explicit higher-generation reset", () => {
      const transition = reduceResearchLiveEvent(
        populatedAttemptOne(),
        reset("5-0", 1, 2),
      );

      expect(transition.state).toMatchObject({
        currentAttemptEpoch: 1,
        currentGeneration: 2,
        draftText: "",
        draftMode: "empty",
        progressStage: "synthesizing",
        currentActivity: RESOLVED_ACTIVITY,
        lastProcessedEventId: streamId("5-0"),
      });
      expect(transition.state).not.toHaveProperty("activityHistory");
    });

    it("uses a higher-generation delta as an implicit reset", () => {
      const transition = reduceResearchLiveEvent(
        populatedAttemptOne(),
        delta("5-0", 1, 3, "置き換え後"),
      );

      expect(transition.state).toMatchObject({
        currentGeneration: 3,
        draftText: "置き換え後",
        draftMode: "visible",
        lastProcessedEventId: streamId("5-0"),
      });
    });

    it("treats a same-generation reset as a no-op without losing the draft", () => {
      const current = populatedAttemptOne();
      const transition = reduceResearchLiveEvent(current, reset("5-0", 1, 1));

      expect(transition.state).toEqual({
        ...current,
        lastProcessedEventId: streamId("5-0"),
      });
    });

    it.each([
      reset("6-0", 2, 1),
      delta("6-0", 2, 1, "古いgeneration"),
    ])("ignores a lower generation $type but consumes its ID", (event) => {
      const current = populatedAttemptTwo();
      const transition = reduceResearchLiveEvent(current, event);

      expect(transition.state).toEqual({
        ...current,
        lastProcessedEventId: streamId("6-0"),
      });
    });

    it("appends a same-generation delta exactly once", () => {
      const current = populatedAttemptOne();
      const appended = reduceResearchLiveEvent(
        current,
        delta("5-0", 1, 1, "の続き"),
      );
      const replayed = reduceResearchLiveEvent(
        appended.state,
        delta("5-0", 1, 1, "の続き"),
      );

      expect(appended.state.draftText).toBe("古い回答の続き");
      expect(replayed).toEqual({
        state: appended.state,
        acceptedTerminal: null,
      });
    });
  });

  describe("Stream ID ordering", () => {
    it.each([
      ["9-0", "10-0"],
      ["1-9", "1-10"],
      ["18446744073709551614-18446744073709551615", "18446744073709551615-0"],
    ])("compares %s < %s as a BigInt pair", (firstId, secondId) => {
      let state = createInitialResearchLiveState();
      state = apply(state, delta(firstId, 1, 1, "A"));

      const transition = reduceResearchLiveEvent(
        state,
        delta(secondId, 1, 1, "B"),
      );

      expect(transition.state.draftText).toBe("AB");
      expect(transition.state.lastProcessedEventId).toEqual(streamId(secondId));
    });

    it.each([
      "4-0",
      "3-999",
    ])("rejects replay or out-of-order ID %s", (replayedId) => {
      const current = populatedAttemptOne();
      const transition = reduceResearchLiveEvent(
        current,
        delta(replayedId, 1, 1, "重複"),
      );

      expect(transition).toEqual({
        state: current,
        acceptedTerminal: null,
      });
    });

    it("advances lastProcessedEventId for a stale generation event", () => {
      const current = populatedAttemptTwo();
      const transition = reduceResearchLiveEvent(
        current,
        delta("6-0", 2, 1, "古い本文"),
      );

      expect(transition.state.draftText).toBe("現在の回答");
      expect(transition.state.currentGeneration).toBe(2);
      expect(transition.state.lastProcessedEventId).toEqual(streamId("6-0"));
    });
  });

  describe("terminal acceptance and absorption", () => {
    it("accepts policy_blocked as an absorbing terminal that clears the draft", () => {
      const transition = reduceResearchLiveEvent(
        populatedAttemptOne(),
        policyBlocked("5-0", 1),
      );

      expect(transition).toMatchObject({
        acceptedTerminal: { status: "policy_blocked" },
        state: {
          terminal: { status: "policy_blocked" },
          draftText: "",
          draftMode: "suppressed",
        },
      });
      expect(
        reduceResearchLiveEvent(transition.state, policyBlocked("6-0", 1)),
      ).toEqual({ state: transition.state, acceptedTerminal: null });
    });

    it.each([
      completed("5-0", 1),
      failed("5-0", 1),
    ])("accepts a current-epoch $status terminal exactly once", (event) => {
      const transition = reduceResearchLiveEvent(populatedAttemptOne(), event);

      expect(transition.state.terminal).toEqual(
        event.status === "completed"
          ? { status: "completed" }
          : { status: "failed", errorCode: "internal_error" },
      );
      expect(transition.acceptedTerminal).toEqual(transition.state.terminal);
      if (event.status === "completed") {
        expect(transition.state.draftText).toBe("古い回答");
        expect(transition.state.draftMode).toBe("visible");
      } else {
        expect(transition.state.draftText).toBe("");
        expect(transition.state.draftMode).toBe("suppressed");
      }
    });

    it("does not accept a newer-ID terminal from a stale epoch", () => {
      const current = populatedAttemptTwo();
      const transition = reduceResearchLiveEvent(current, completed("6-0", 1));

      expect(transition.state).toEqual({
        ...current,
        lastProcessedEventId: streamId("6-0"),
      });
      expect(transition.acceptedTerminal).toBeNull();
    });

    it("does not accept a replayed terminal", () => {
      const terminalState = reduceResearchLiveEvent(
        populatedAttemptOne(),
        completed("5-0", 1),
      ).state;

      expect(
        reduceResearchLiveEvent(terminalState, completed("5-0", 1)),
      ).toEqual({ state: terminalState, acceptedTerminal: null });
    });

    it.each([
      marker("6-0", 2),
      stage("6-0", 1, "planning"),
      activity("6-0", 1, NEW_ACTIVITY),
      delta("6-0", 1, 2, "後続"),
      reset("6-0", 1, 2),
      failed("6-0", 1),
    ])("keeps terminal state absorbing for a later $type", (event) => {
      const terminalState = reduceResearchLiveEvent(
        populatedAttemptOne(),
        completed("5-0", 1),
      ).state;

      expect(reduceResearchLiveEvent(terminalState, event)).toEqual({
        state: terminalState,
        acceptedTerminal: null,
      });
    });
  });

  describe("draft suppression", () => {
    it("suppresses a visible draft without changing attempt progress", () => {
      const current = populatedAttemptOne();

      expect(suppressResearchLiveDraft(current)).toEqual({
        ...current,
        draftText: "",
        draftMode: "suppressed",
      });
    });

    it("does not revive a suppressed draft when completed is accepted", () => {
      const suppressed = suppressResearchLiveDraft(populatedAttemptOne());
      const transition = reduceResearchLiveEvent(
        suppressed,
        completed("5-0", 1),
      );

      expect(transition.state.draftText).toBe("");
      expect(transition.state.draftMode).toBe("suppressed");
      expect(transition.acceptedTerminal).toEqual({ status: "completed" });
    });
  });
});
