import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createResearchRunLiveController } from "./controller";
import type { ResearchLiveStage } from "./events";

const RUN_ID = "00000000-0000-4000-a000-000000000010";
const EVENT_SOURCE_CONNECTING = 0;
const EVENT_SOURCE_OPEN = 1;
const EVENT_SOURCE_CLOSED = 2;
const PUBLIC_EVENTS = [
  "attempt.started",
  "stage",
  "activity",
  "answer.delta",
  "answer.reset",
  "terminal",
] as const;

type PollRunResult =
  | {
      kind: "run";
      run: {
        status: "queued" | "running" | "completed" | "failed";
        attemptEpoch: unknown;
        errorCode:
          | "generation_unavailable"
          | "internal_error"
          | "enqueue_failed"
          | "stale"
          | "cancelled"
          | null;
      };
    }
  | { kind: "http-error"; status: 401 | 403 | 404 }
  | { kind: "unavailable" };

type PollRun = (runId: string, signal: AbortSignal) => Promise<PollRunResult>;
type RequestRefresh = () => Promise<void>;

type PolicyBlockedPollRunResult = {
  kind: "run";
  run: {
    status: "policy_blocked";
    attemptEpoch: number | null;
    errorCode: null;
  };
};
type PolicyBlockedPollRun = (
  runId: string,
  signal: AbortSignal,
) => Promise<PolicyBlockedPollRunResult>;

type PollErrorCode = Exclude<
  Extract<PollRunResult, { kind: "run" }>["run"]["errorCode"],
  undefined
>;

function runResult(
  status: "queued" | "running" | "completed" | "failed" = "running",
  _progressStage: ResearchLiveStage | null = null,
  errorCode: PollErrorCode = null,
  _recentEvents: readonly unknown[] = [],
  attemptEpoch: unknown = 1,
): PollRunResult {
  return {
    kind: "run",
    run: { status, attemptEpoch, errorCode },
  };
}

function policyBlockedRunResult(
  attemptEpoch: number | null = 1,
): PolicyBlockedPollRunResult {
  return {
    kind: "run",
    run: {
      status: "policy_blocked",
      attemptEpoch,
      errorCode: null,
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

class FakeEventSource {
  readyState: number = EVENT_SOURCE_CONNECTING;
  closeCount = 0;
  readonly listenerTypes: string[] = [];
  private readonly listeners = new Map<
    string,
    Set<EventListenerOrEventListenerObject>
  >();

  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void {
    this.listenerTypes.push(type);
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closeCount += 1;
    this.readyState = EVENT_SOURCE_CLOSED;
  }

  open(): void {
    this.readyState = EVENT_SOURCE_OPEN;
    this.dispatch("open", new Event("open"));
  }

  reconnecting(): void {
    this.readyState = EVENT_SOURCE_CONNECTING;
    this.dispatch("error", new Event("error"));
  }

  closed(): void {
    this.readyState = EVENT_SOURCE_CLOSED;
    this.dispatch("error", new Event("error"));
  }

  emit(eventName: string, data: unknown, lastEventId: string): void {
    this.dispatch(
      eventName,
      new MessageEvent(eventName, {
        data: JSON.stringify(data),
        lastEventId,
      }),
    );
  }

  private dispatch(type: string, event: Event): void {
    for (const listener of this.listeners.get(type) ?? []) {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    }
  }
}

class FakeVisibility {
  hidden = false;
  private readonly listeners = new Set<() => void>();

  isHidden = () => this.hidden;

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  setHidden(hidden: boolean): void {
    this.hidden = hidden;
    for (const listener of this.listeners) listener();
  }
}

function createHarness(
  pollRun?: ReturnType<typeof vi.fn<PollRun>>,
  initialStatus: "queued" | "running" = "running",
  requestRefresh?: ReturnType<typeof vi.fn<RequestRefresh>>,
  useRuntimePoll = false,
) {
  const sources: FakeEventSource[] = [];
  const createEventSource = vi.fn((_url: string) => {
    const source = new FakeEventSource();
    sources.push(source);
    return source as unknown as EventSource;
  });
  const actualRequestRefresh =
    requestRefresh ?? vi.fn<RequestRefresh>().mockResolvedValue(undefined);
  const visibility = new FakeVisibility();
  const actualPollRun =
    pollRun ?? vi.fn<PollRun>().mockResolvedValue(runResult());
  const controllerOptions = {
    runId: RUN_ID,
    initialStatus,
    ...(useRuntimePoll ? {} : { pollRun: actualPollRun }),
    createEventSource,
    requestRefresh: actualRequestRefresh,
    refresh: actualRequestRefresh,
    visibility,
  };
  const controller = createResearchRunLiveController(
    controllerOptions as unknown as Parameters<
      typeof createResearchRunLiveController
    >[0],
  );
  const notify = vi.fn();
  const unsubscribe = controller.subscribe(notify);

  return {
    controller,
    pollRun: actualPollRun,
    createEventSource,
    requestRefresh: actualRequestRefresh,
    refresh: actualRequestRefresh,
    visibility,
    sources,
    source: sources[0] as FakeEventSource,
    notify,
    unsubscribe,
  };
}

async function flushPromises(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("createResearchRunLiveController", () => {
  describe("polling lifecycle", () => {
    it("does not poll while connecting, live, or reconnecting", async () => {
      const harness = createHarness();

      expect(harness.pollRun).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(10_000);
      expect(harness.pollRun).not.toHaveBeenCalled();

      harness.source.open();
      await vi.advanceTimersByTimeAsync(10_000);
      expect(harness.pollRun).not.toHaveBeenCalled();
      expect(harness.controller.getSnapshot().connectionMode).toBe("live");

      harness.source.reconnecting();
      await vi.advanceTimersByTimeAsync(10_000);
      expect(harness.pollRun).not.toHaveBeenCalled();
      expect(harness.controller.getSnapshot().connectionMode).toBe(
        "reconnecting",
      );

      harness.unsubscribe();
    });

    it("polls after EventSource CLOSED and schedules success responses every two seconds", async () => {
      const harness = createHarness();
      harness.source.closed();

      expect(harness.pollRun).toHaveBeenCalledTimes(1);
      expect(harness.pollRun).toHaveBeenCalledWith(
        RUN_ID,
        expect.any(AbortSignal),
      );
      await flushPromises();
      await vi.advanceTimersByTimeAsync(1999);
      expect(harness.pollRun).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(harness.pollRun).toHaveBeenCalledTimes(2);

      harness.unsubscribe();
    });

    it("backs unavailable polling off at 4, 8, then 10 seconds", async () => {
      const pollRun = vi
        .fn<PollRun>()
        .mockResolvedValue({ kind: "unavailable" });
      const harness = createHarness(pollRun);
      harness.source.closed();

      await flushPromises();
      await vi.advanceTimersByTimeAsync(3999);
      expect(pollRun).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(pollRun).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(8000);
      expect(pollRun).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(10000);
      expect(pollRun).toHaveBeenCalledTimes(4);
      await vi.advanceTimersByTimeAsync(10000);
      expect(pollRun).toHaveBeenCalledTimes(5);

      harness.unsubscribe();
    });

    it("does not overlap polling requests and aborts without scheduling after cleanup", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.closed();
      const signal = pollRun.mock.calls[0]?.[1];

      await vi.advanceTimersByTimeAsync(30000);
      expect(pollRun).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
      expect(signal?.aborted).toBe(true);
      pending.resolve(runResult());
      await flushPromises();
      await vi.advanceTimersByTimeAsync(30000);
      expect(pollRun).toHaveBeenCalledTimes(1);
    });

    it.each([
      401, 403, 404,
    ] as const)("closes live resources and never restarts after HTTP %s", async (status) => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "draft" },
        "1-0",
      );
      harness.source.closed();

      pending.resolve({ kind: "http-error", status });
      await flushPromises();

      expect(harness.refresh).toHaveBeenCalledTimes(1);
      expect(harness.source.closeCount).toBe(1);
      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionMode: "polling-only",
        liveState: { draftText: "", draftMode: "suppressed" },
      });
      await vi.advanceTimersByTimeAsync(30000);
      harness.visibility.setHidden(true);
      harness.visibility.setHidden(false);
      await vi.advanceTimersByTimeAsync(30000);
      expect(pollRun).toHaveBeenCalledTimes(1);
      expect(harness.createEventSource).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
    });
  });

  describe("SSE projection and connection state", () => {
    it("creates one EventSource and registers only the six public event listeners", () => {
      const harness = createHarness();

      expect(harness.createEventSource).toHaveBeenCalledTimes(1);
      expect(harness.createEventSource).toHaveBeenCalledWith(
        `/api/research/runs/${RUN_ID}/events`,
      );
      for (const eventName of PUBLIC_EVENTS) {
        expect(harness.source.listenerTypes).toContain(eventName);
      }
      expect(
        harness.source.listenerTypes.filter((name) =>
          PUBLIC_EVENTS.some((eventName) => eventName === name),
        ),
      ).toHaveLength(PUBLIC_EVENTS.length);

      harness.unsubscribe();
    });

    it("passes a valid public event through parser and reducer", () => {
      const harness = createHarness();
      harness.source.emit(
        "stage",
        { attemptEpoch: 1, stage: "answering" },
        "1-0",
      );

      expect(harness.controller.getSnapshot().liveState).toMatchObject({
        currentAttemptEpoch: 1,
        progressStage: "answering",
        lastProcessedEventId: {
          raw: "1-0",
          milliseconds: 1n,
          sequence: 0n,
        },
      });

      harness.unsubscribe();
    });

    it("drops event-local invalid data without changing state or closing", () => {
      const harness = createHarness();
      const before = harness.controller.getSnapshot();
      harness.source.emit(
        "stage",
        { attemptEpoch: 1, stage: "private" },
        "1-0",
      );

      expect(harness.controller.getSnapshot()).toBe(before);
      expect(harness.source.closeCount).toBe(0);

      harness.unsubscribe();
    });

    it("moves open to live and CONNECTING errors to reconnecting on the same instance", () => {
      const harness = createHarness();
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "draft" },
        "1-0",
      );
      const liveState = harness.controller.getSnapshot().liveState;

      harness.source.open();
      expect(harness.controller.getSnapshot().connectionMode).toBe("live");
      harness.source.reconnecting();

      expect(harness.controller.getSnapshot().connectionMode).toBe(
        "reconnecting",
      );
      expect(harness.controller.getSnapshot().liveState).toBe(liveState);
      expect(harness.createEventSource).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
    });

    it("moves a CLOSED error to polling-only and suppresses the draft without reconnecting", () => {
      const harness = createHarness();
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "draft" },
        "1-0",
      );

      harness.source.closed();

      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionMode: "polling-only",
        liveState: { draftText: "", draftMode: "suppressed" },
      });
      expect(harness.pollRun).toHaveBeenCalledTimes(1);
      expect(harness.createEventSource).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
    });

    it("treats an invalid Stream ID as protocol failure and polling-only", () => {
      const harness = createHarness();
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "draft" },
        "1-0",
      );
      harness.source.emit("stage", { attemptEpoch: 1, stage: "planning" }, "");

      expect(harness.source.closeCount).toBe(1);
      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionMode: "polling-only",
        liveState: { draftText: "", draftMode: "suppressed" },
      });
      expect(harness.pollRun).toHaveBeenCalledTimes(1);
      expect(harness.createEventSource).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
    });
  });

  describe("SSE and polling state integration", () => {
    it("promotes a queued run to running after accepting nonterminal SSE", async () => {
      const pollRun = vi.fn<PollRun>().mockResolvedValue(runResult("queued"));
      const harness = createHarness(pollRun, "queued");

      await flushPromises();
      expect(harness.controller.getSnapshot().runStatus).toBe("queued");
      harness.source.emit("attempt.started", { attemptEpoch: 1 }, "1-0");
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "draft" },
        "2-0",
      );

      expect(harness.controller.getSnapshot()).toMatchObject({
        runStatus: "running",
        liveState: { draftText: "draft", draftMode: "visible" },
      });

      harness.unsubscribe();
    });

    it("keeps SSE activity after polling-only instead of replacing it with List activity", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.emit(
        "activity",
        {
          attemptEpoch: 1,
          activity: {
            type: "evidence_collection.external_search_hits_fetched",
            taskIndex: 0,
            hitCount: 1,
          },
        },
        "1-0",
      );
      harness.source.closed();

      pending.resolve(
        runResult("running", "evidence_collection", null, [
          {
            type: "evidence_collection.external_search_hits_fetched",
            ts: "2026-07-13T00:00:00Z",
            taskIndex: 1,
            hitCount: 12,
          },
          { type: "future.event", secret: "discarded" },
        ]),
      );
      await flushPromises();

      expect(
        harness.controller.getSnapshot().liveState.currentActivity,
      ).toEqual({
        type: "evidence_collection.external_search_hits_fetched",
        taskIndex: 0,
        hitCount: 1,
      });

      harness.unsubscribe();
    });

    it("does not apply polling progress while connecting before the first SSE event", async () => {
      const pollRun = vi
        .fn<PollRun>()
        .mockResolvedValue(runResult("running", "evidence_collection"));
      const harness = createHarness(pollRun);

      await flushPromises();
      expect(
        harness.controller.getSnapshot().liveState.progressStage,
      ).toBeNull();

      harness.unsubscribe();
    });

    it.each([
      "live",
      "reconnecting",
    ] as const)("does not apply a same-attempt forward poll while %s", async (mode) => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.open();
      harness.source.emit(
        "stage",
        { attemptEpoch: 1, stage: "planning" },
        "1-0",
      );
      const sseActivity = {
        type: "evidence_review.selected" as const,
        evidenceCount: 2,
      };
      harness.source.emit(
        "activity",
        { attemptEpoch: 1, activity: sseActivity },
        "2-0",
      );
      if (mode === "reconnecting") harness.source.reconnecting();

      pending.resolve(
        runResult(
          "running",
          "evidence_collection",
          null,
          [
            {
              type: "evidence_collection.external_search_hits_fetched",
              ts: "2026-07-13T00:00:00Z",
              taskIndex: 1,
              hitCount: 4,
            },
          ],
          1,
        ),
      );
      await flushPromises();

      expect(pollRun).not.toHaveBeenCalled();
      expect(harness.controller.getSnapshot().liveState.progressStage).toBe(
        "planning",
      );
      expect(
        harness.controller.getSnapshot().liveState.currentActivity,
      ).toEqual(sseActivity);

      harness.unsubscribe();
    });

    it("does not regress SSE progress or derive activity from a rejected polling stage", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.emit(
        "stage",
        { attemptEpoch: 1, stage: "answering" },
        "1-0",
      );
      harness.source.closed();

      pending.resolve(
        runResult(
          "running",
          "evidence_collection",
          null,
          [
            {
              type: "evidence_review.selected",
              ts: "2026-07-13T00:00:00Z",
              evidenceCount: 4,
            },
          ],
          1,
        ),
      );
      await flushPromises();

      expect(harness.controller.getSnapshot().liveState).toMatchObject({
        progressStage: "answering",
        currentActivity: null,
      });

      harness.unsubscribe();
    });

    it("applies delayed same-attempt SSE evidence_collection even when polling claimed answering", async () => {
      const pollRun = vi
        .fn<PollRun>()
        .mockResolvedValue(runResult("running", "answering", null, [], 1));
      const harness = createHarness(pollRun);

      await flushPromises();
      expect(
        harness.controller.getSnapshot().liveState.progressStage,
      ).toBeNull();
      harness.source.emit(
        "stage",
        { attemptEpoch: 1, stage: "evidence_collection" },
        "1-0",
      );

      expect(harness.controller.getSnapshot().liveState).toMatchObject({
        progressStage: "evidence_collection",
        lastProcessedEventId: {
          raw: "1-0",
          milliseconds: 1n,
          sequence: 0n,
        },
      });

      harness.unsubscribe();
    });

    it("adopts a valid polling epoch when no SSE attempt has been observed", async () => {
      const pollRun = vi
        .fn<PollRun>()
        .mockResolvedValue(
          runResult("running", "evidence_collection", null, [], 2),
        );
      const harness = createHarness(pollRun);
      harness.source.closed();

      await flushPromises();

      expect(harness.controller.getSnapshot().liveState).toMatchObject({
        currentAttemptEpoch: 2,
        progressStage: null,
      });

      harness.unsubscribe();
    });

    it("resets for a higher polling epoch without applying its List activity", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.emit(
        "stage",
        { attemptEpoch: 1, stage: "answering" },
        "1-0",
      );
      harness.source.emit(
        "activity",
        {
          attemptEpoch: 1,
          activity: {
            type: "evidence_review.selected",
            evidenceCount: 2,
          },
        },
        "2-0",
      );
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 2, text: "old draft" },
        "3-0",
      );
      harness.source.closed();

      pending.resolve(
        runResult(
          "running",
          "planning",
          null,
          [
            {
              type: "evidence_collection.internal_search_started",
              ts: "2026-07-13T00:00:00Z",
              queryCount: 2,
            },
          ],
          2,
        ),
      );
      await flushPromises();

      expect(harness.controller.getSnapshot().liveState).toMatchObject({
        currentAttemptEpoch: 2,
        currentGeneration: null,
        progressStage: null,
        currentActivity: null,
        draftText: "",
        draftMode: "empty",
        lastProcessedEventId: {
          raw: "3-0",
          milliseconds: 3n,
          sequence: 0n,
        },
      });

      harness.unsubscribe();
    });

    it("ignores lower-epoch polling stage and activity", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      const currentActivity = {
        type: "evidence_collection.external_search_hits_fetched" as const,
        taskIndex: 1,
        hitCount: 3,
      };
      harness.source.emit(
        "stage",
        { attemptEpoch: 2, stage: "evidence_collection" },
        "1-0",
      );
      harness.source.emit(
        "activity",
        { attemptEpoch: 2, activity: currentActivity },
        "2-0",
      );
      harness.source.closed();

      pending.resolve(
        runResult(
          "running",
          "answering",
          null,
          [
            {
              type: "evidence_collection.internal_search_started",
              ts: "2026-07-13T00:00:00Z",
              queryCount: 2,
            },
          ],
          1,
        ),
      );
      await flushPromises();

      expect(harness.controller.getSnapshot().liveState).toMatchObject({
        currentAttemptEpoch: 2,
        progressStage: "evidence_collection",
        currentActivity,
      });

      harness.unsubscribe();
    });

    it("keeps active run status monotonic when polling stage cannot be merged", async () => {
      const pollRun = vi
        .fn<PollRun>()
        .mockResolvedValueOnce(runResult("running", "answering", null, [], 0))
        .mockResolvedValueOnce(runResult("queued", "planning", null, [], 0));
      const harness = createHarness(pollRun, "queued");
      harness.source.closed();

      await flushPromises();
      await vi.advanceTimersByTimeAsync(2_000);
      await flushPromises();

      expect(harness.controller.getSnapshot()).toMatchObject({
        runStatus: "running",
        liveState: { progressStage: null, currentAttemptEpoch: null },
      });

      harness.unsubscribe();
    });

    it.each([
      ["missing", {}],
      ["null", { attemptEpoch: null }],
      ["zero", { attemptEpoch: 0 }],
      ["negative", { attemptEpoch: -1 }],
      ["fraction", { attemptEpoch: 1.5 }],
      ["string", { attemptEpoch: "1" }],
      ["boolean", { attemptEpoch: true }],
      ["unsafe", { attemptEpoch: Number.MAX_SAFE_INTEGER + 1 }],
    ] as const)("does not merge stage or activity from a %s epoch in the runtime poll parser", async (_label, epochField) => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "running",
            progressStage: "answering",
            errorCode: null,
            ...epochField,
          }),
          { status: 200 },
        ),
      );
      vi.stubGlobal("fetch", fetchMock);
      const harness = createHarness(undefined, "queued", undefined, true);
      const currentActivity = {
        type: "evidence_collection.internal_search_started" as const,
        queryCount: 2,
      };
      harness.source.emit(
        "stage",
        { attemptEpoch: 1, stage: "planning" },
        "1-0",
      );
      harness.source.emit(
        "activity",
        { attemptEpoch: 1, activity: currentActivity },
        "2-0",
      );
      harness.source.closed();

      await flushPromises();

      expect(harness.controller.getSnapshot()).toMatchObject({
        runStatus: "running",
        liveState: {
          currentAttemptEpoch: 1,
          progressStage: "planning",
          currentActivity,
        },
      });
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/research/runs/${RUN_ID}`,
        expect.objectContaining({ cache: "no-store" }),
      );

      harness.unsubscribe();
    });

    it("finalizes a polling terminal even when its epoch cannot merge progress", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "completed",
            progressStage: "answering",
            errorCode: null,
            attemptEpoch: 0,
          }),
          { status: 200 },
        ),
      );
      vi.stubGlobal("fetch", fetchMock);
      const harness = createHarness(undefined, "queued", undefined, true);
      harness.source.closed();

      await vi.waitFor(() => {
        expect(harness.controller.getSnapshot()).toMatchObject({
          runStatus: "completed",
          connectionMode: "finalizing",
          liveState: { terminal: { status: "completed" } },
        });
      });

      harness.unsubscribe();
    });

    it("does not recover stage from polling after a new attempt begins with an activity event", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.emit(
        "stage",
        { attemptEpoch: 1, stage: "answering" },
        "1-0",
      );
      harness.source.emit(
        "activity",
        {
          attemptEpoch: 2,
          activity: {
            type: "evidence_collection.internal_search_started",
            queryCount: 1,
          },
        },
        "2-0",
      );

      expect(harness.controller.getSnapshot().liveState).toMatchObject({
        currentAttemptEpoch: 2,
        progressStage: null,
      });

      harness.source.closed();
      pending.resolve(runResult("running", "evidence_collection", null, [], 2));
      await flushPromises();

      expect(
        harness.controller.getSnapshot().liveState.progressStage,
      ).toBeNull();

      harness.unsubscribe();
    });

    it("does not apply polling progress in polling-only mode", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.closed();

      pending.resolve(runResult("running", "evidence_collection"));
      await flushPromises();
      expect(
        harness.controller.getSnapshot().liveState.progressStage,
      ).toBeNull();

      harness.unsubscribe();
    });

    it("accepts a polling terminal without an SSE epoch", async () => {
      const pollRun = vi
        .fn<PollRun>()
        .mockResolvedValue(runResult("completed"));
      const harness = createHarness(pollRun);
      harness.source.closed();

      await flushPromises();
      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionMode: "finalizing",
        runStatus: "completed",
      });
      expect(harness.refresh).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
    });

    it("does not apply a delayed active response after finalization", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.emit(
        "terminal",
        { attemptEpoch: 1, status: "completed" },
        "1-0",
      );

      pending.resolve(runResult("running", "evidence_collection"));
      await flushPromises();
      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionMode: "finalizing",
        runStatus: "completed",
        liveState: { progressStage: null },
      });

      harness.unsubscribe();
    });
  });

  describe("progress stage vocabulary", () => {
    it.each([
      "safety_check",
      "context_resolution",
      "planning",
      "evidence_collection",
      "evidence_review",
      "answering",
    ] as const)("does not apply a polling report of the %s stage", async (stageValue) => {
      const pollRun = vi
        .fn<PollRun>()
        .mockResolvedValue(runResult("running", stageValue));
      const harness = createHarness(pollRun);

      await flushPromises();
      expect(
        harness.controller.getSnapshot().liveState.progressStage,
      ).toBeNull();

      harness.unsubscribe();
    });

    it.each([
      "retrieving",
      "synthesizing",
    ])("rejects the retired %s stage from a polling report and leaves progress unset", async (retiredStageValue) => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "running",
            progressStage: retiredStageValue,
            errorCode: null,
            attemptEpoch: 1,
          }),
          { status: 200 },
        ),
      );
      vi.stubGlobal("fetch", fetchMock);
      const harness = createHarness(undefined, "queued", undefined, true);
      harness.source.closed();

      await vi.waitFor(() => {
        expect(harness.controller.getSnapshot().runStatus).toBe("running");
      });
      expect(
        harness.controller.getSnapshot().liveState.progressStage,
      ).toBeNull();

      harness.unsubscribe();
    });
  });

  describe("terminal and finalization", () => {
    it("converges an SSE policy_blocked terminal without finalizing or failing", () => {
      const harness = createHarness();
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "破棄する下書き" },
        "1-0",
      );
      harness.source.emit(
        "terminal",
        {
          attemptEpoch: 1,
          status: "policy_blocked",
          blockReason: "provider_safety_filter",
        },
        "2-0",
      );

      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionMode: "terminal",
        runStatus: "policy_blocked",
        liveState: {
          terminal: { status: "policy_blocked" },
          draftText: "",
          draftMode: "suppressed",
        },
      });
      expect(
        harness.controller.getSnapshot().liveState.terminal,
      ).not.toHaveProperty("errorCode");
      expect(harness.source.closeCount).toBe(1);
      expect(harness.refresh).toHaveBeenCalledTimes(1);

      harness.source.emit(
        "terminal",
        { attemptEpoch: 1, status: "policy_blocked" },
        "3-0",
      );
      expect(harness.refresh).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
    });

    it("converges a polling policy_blocked terminal and suppresses an SSE draft", async () => {
      const pending = deferred<PolicyBlockedPollRunResult>();
      const pollRun = vi
        .fn<PolicyBlockedPollRun>()
        .mockReturnValue(pending.promise);
      const harness = createHarness(
        pollRun as unknown as ReturnType<typeof vi.fn<PollRun>>,
      );
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "poll前の下書き" },
        "1-0",
      );
      harness.source.closed();

      pending.resolve(policyBlockedRunResult());
      await flushPromises();

      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionMode: "terminal",
        runStatus: "policy_blocked",
        liveState: {
          terminal: { status: "policy_blocked" },
          draftText: "",
          draftMode: "suppressed",
        },
      });
      expect(harness.refresh).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
    });

    it.each([
      ["completed", null, { status: "completed" }],
      ["failed", null, { status: "failed", errorCode: "internal_error" }],
      ["failed", "cancelled", { status: "failed", errorCode: "cancelled" }],
    ] as const)("keeps polling %s terminal details as the same presentation input as SSE", async (status, errorCode, expectedTerminal) => {
      const pollRun = vi
        .fn<PollRun>()
        .mockResolvedValue(runResult(status, null, errorCode));
      const pollingHarness = createHarness(pollRun);
      pollingHarness.source.closed();
      await flushPromises();

      const sseHarness = createHarness();
      sseHarness.source.emit(
        "terminal",
        { attemptEpoch: 1, status, errorCode },
        "1-0",
      );

      expect(pollingHarness.controller.getSnapshot()).toMatchObject({
        connectionMode: "finalizing",
        runStatus: status,
        liveState: { terminal: expectedTerminal },
      });
      expect(
        pollingHarness.controller.getSnapshot().liveState.terminal,
      ).toEqual(sseHarness.controller.getSnapshot().liveState.terminal);

      pollingHarness.unsubscribe();
      sseHarness.unsubscribe();
    });

    it("does not finalize for a stale-epoch or replayed terminal", () => {
      const harness = createHarness();
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 2, generation: 1, text: "draft" },
        "1-0",
      );
      harness.source.emit(
        "terminal",
        { attemptEpoch: 1, status: "completed" },
        "2-0",
      );
      harness.source.emit(
        "terminal",
        { attemptEpoch: 2, status: "completed" },
        "1-0",
      );

      expect(harness.refresh).not.toHaveBeenCalled();
      expect(harness.source.closeCount).toBe(0);
      expect(harness.controller.getSnapshot().connectionMode).toBe(
        "connecting",
      );

      harness.unsubscribe();
    });

    it.each([
      ["completed", null, "draft", "visible"],
      ["failed", "internal_error", "", "suppressed"],
      ["failed", "cancelled", "", "suppressed"],
    ] as const)("handles accepted %s terminal with draft mode %s", (status, errorCode, draftText, draftMode) => {
      const harness = createHarness();
      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "draft" },
        "1-0",
      );
      harness.source.emit(
        "terminal",
        { attemptEpoch: 1, status, errorCode },
        "2-0",
      );

      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionMode: "finalizing",
        runStatus: status,
        liveState: { draftText, draftMode },
      });
      expect(harness.source.closeCount).toBe(1);
      expect(harness.refresh).toHaveBeenCalledTimes(1);

      harness.unsubscribe();
    });

    it("finalizes once when accepted SSE and polling terminal race", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.emit(
        "terminal",
        { attemptEpoch: 1, status: "completed" },
        "1-0",
      );
      pending.resolve(runResult("completed"));
      await flushPromises();

      expect(harness.refresh).toHaveBeenCalledTimes(1);
      expect(harness.controller.getSnapshot().connectionMode).toBe(
        "finalizing",
      );

      harness.unsubscribe();
    });

    it("waits for each refresh commit ack before retrying and ignores settle after dispose", async () => {
      const pendingPoll = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pendingPoll.promise);
      const firstRefresh = deferred<void>();
      const secondRefresh = deferred<void>();
      const requestRefresh = vi
        .fn<RequestRefresh>()
        .mockReturnValueOnce(firstRefresh.promise)
        .mockReturnValueOnce(secondRefresh.promise);
      const harness = createHarness(pollRun, "running", requestRefresh);

      pendingPoll.resolve(runResult("completed"));
      harness.source.emit(
        "terminal",
        { attemptEpoch: 1, status: "completed" },
        "1-0",
      );
      await flushPromises();
      harness.visibility.setHidden(true);
      harness.visibility.setHidden(false);

      expect(requestRefresh).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(12_000);
      expect(requestRefresh).toHaveBeenCalledTimes(1);

      firstRefresh.resolve(undefined);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(1_999);
      expect(requestRefresh).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(requestRefresh).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(10_001);
      expect(requestRefresh).toHaveBeenCalledTimes(2);

      harness.unsubscribe();
      secondRefresh.resolve(undefined);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(requestRefresh).toHaveBeenCalledTimes(2);
    });

    it("retries 2, 4, 8, and capped 10 seconds after each refresh ack", async () => {
      const harness = createHarness();
      harness.source.emit(
        "terminal",
        { attemptEpoch: 1, status: "completed" },
        "1-0",
      );

      expect(harness.refresh).toHaveBeenCalledTimes(1);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(2000);
      expect(harness.refresh).toHaveBeenCalledTimes(2);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(4000);
      expect(harness.refresh).toHaveBeenCalledTimes(3);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(8000);
      expect(harness.refresh).toHaveBeenCalledTimes(4);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(10000);
      expect(harness.refresh).toHaveBeenCalledTimes(5);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(10000);
      expect(harness.refresh).toHaveBeenCalledTimes(6);

      harness.unsubscribe();
      await vi.advanceTimersByTimeAsync(30000);
      expect(harness.refresh).toHaveBeenCalledTimes(6);
    });
  });

  describe("visibility and cleanup", () => {
    it("aborts an in-flight polling-only request while hidden, ignores its response, and polls immediately when visible", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi
        .fn<PollRun>()
        .mockReturnValueOnce(pending.promise)
        .mockResolvedValue(runResult("running", "planning"));
      const harness = createHarness(pollRun);
      harness.source.closed();
      const hiddenRequestSignal = pollRun.mock.calls[0]?.[1];

      harness.visibility.setHidden(true);

      expect(hiddenRequestSignal?.aborted).toBe(true);
      pending.resolve(runResult("running", "evidence_collection"));
      await flushPromises();
      expect(
        harness.controller.getSnapshot().liveState.progressStage,
      ).toBeNull();
      expect(pollRun).toHaveBeenCalledTimes(1);

      harness.visibility.setHidden(false);
      expect(pollRun).toHaveBeenCalledTimes(2);
      expect(pollRun.mock.calls[1]?.[0]).toBe(RUN_ID);
      expect(pollRun.mock.calls[1]?.[1]).not.toBe(hiddenRequestSignal);
      await flushPromises();
      expect(
        harness.controller.getSnapshot().liveState.progressStage,
      ).toBeNull();

      harness.unsubscribe();
    });

    it("does not poll on hide or show while live, and keeps EventSource", async () => {
      const harness = createHarness();
      harness.source.open();
      harness.visibility.setHidden(true);
      await vi.advanceTimersByTimeAsync(10_000);

      expect(harness.pollRun).not.toHaveBeenCalled();
      expect(harness.source.closeCount).toBe(0);
      expect(harness.createEventSource).toHaveBeenCalledTimes(1);

      harness.visibility.setHidden(false);
      await vi.advanceTimersByTimeAsync(10_000);
      expect(harness.pollRun).not.toHaveBeenCalled();
      expect(harness.source.closeCount).toBe(0);

      harness.unsubscribe();
    });

    it("pauses polling-only while hidden and polls immediately when visible", async () => {
      const harness = createHarness();
      harness.source.closed();
      await flushPromises();
      harness.visibility.setHidden(true);
      await vi.advanceTimersByTimeAsync(10000);

      expect(harness.pollRun).toHaveBeenCalledTimes(1);
      expect(harness.createEventSource).toHaveBeenCalledTimes(1);

      harness.visibility.setHidden(false);
      expect(harness.pollRun).toHaveBeenCalledTimes(2);

      harness.unsubscribe();
    });

    it("does not schedule a settled refresh while hidden and starts one attempt when visible", async () => {
      const firstRefresh = deferred<void>();
      const secondRefresh = deferred<void>();
      const requestRefresh = vi
        .fn<RequestRefresh>()
        .mockReturnValueOnce(firstRefresh.promise)
        .mockReturnValueOnce(secondRefresh.promise);
      const harness = createHarness(undefined, "running", requestRefresh);
      harness.source.emit(
        "terminal",
        { attemptEpoch: 1, status: "completed" },
        "1-0",
      );
      harness.visibility.setHidden(true);
      firstRefresh.resolve(undefined);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(30000);
      expect(requestRefresh).toHaveBeenCalledTimes(1);

      harness.visibility.setHidden(false);
      expect(requestRefresh).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(30000);
      expect(requestRefresh).toHaveBeenCalledTimes(2);

      harness.unsubscribe();
      secondRefresh.resolve(undefined);
    });

    it("removes listeners, closes SSE, aborts polling, and ignores old callbacks after cleanup", async () => {
      const pending = deferred<PollRunResult>();
      const pollRun = vi.fn<PollRun>().mockReturnValue(pending.promise);
      const harness = createHarness(pollRun);
      harness.source.closed();
      const before = harness.controller.getSnapshot();

      harness.unsubscribe();
      expect(harness.source.closeCount).toBe(1);
      expect(pollRun.mock.calls[0]?.[1].aborted).toBe(true);

      harness.source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "late" },
        "1-0",
      );
      pending.resolve(runResult("running", "evidence_collection"));
      await flushPromises();
      await vi.advanceTimersByTimeAsync(30000);

      expect(harness.controller.getSnapshot()).toBe(before);
      expect(pollRun).toHaveBeenCalledTimes(1);
    });
  });
});
