import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ResearchAssistantMessage,
  ResearchMessageRun,
  ResearchThreadDetail,
  ResearchUserMessage,
} from "@/types/types.gen";
import { ResearchThreadView } from "./ResearchThreadView";

const THREAD_ONE = "00000000-0000-4000-a000-000000000001";
const THREAD_TWO = "00000000-0000-4000-a000-000000000002";
const RUN_ONE = "00000000-0000-4000-a000-000000000010";
const RUN_TWO = "00000000-0000-4000-a000-000000000020";

interface ScrollCapture {
  containerRef: { readonly current: HTMLElement | null };
  contentRevision: number;
  failedContractionRevision?: number;
}

const mocks = vi.hoisted(() => ({
  refresh: vi.fn(),
  scrollProps: [] as ScrollCapture[],
  renderActualScroll: false,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: mocks.refresh }),
}));

vi.mock("./DeleteThreadButton", () => ({
  DeleteThreadButton: () => <button type="button">削除</button>,
}));

vi.mock("./ResearchComposer", () => ({
  ResearchComposer: ({ activeRunId }: { activeRunId: string | null }) => (
    <div data-testid="composer-run-id">{activeRunId ?? "none"}</div>
  ),
}));

vi.mock("./ResearchLiveScrollButton", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("./ResearchLiveScrollButton")>();
  return {
    ResearchLiveScrollButton: (props: ScrollCapture) => {
      mocks.scrollProps.push(props);
      return mocks.renderActualScroll ? (
        <actual.ResearchLiveScrollButton {...props} />
      ) : null;
    },
  };
});

type Listener = EventListenerOrEventListenerObject;

class FakeEventSource {
  static readonly instances: FakeEventSource[] = [];
  readyState = 0;
  closeCount = 0;
  readonly url: string;
  private readonly listeners = new Map<string, Set<Listener>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.readyState = 2;
    this.closeCount += 1;
  }

  open(): void {
    this.readyState = 1;
    this.dispatch("open", new Event("open"));
  }

  reconnecting(): void {
    this.readyState = 0;
    this.dispatch("error", new Event("error"));
  }

  closed(): void {
    this.readyState = 2;
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

function run(
  runId: string,
  status: ResearchMessageRun["status"] = "running",
  errorCode: ResearchMessageRun["errorCode"] = null,
): ResearchMessageRun {
  return {
    runId,
    status,
    errorCode,
    progressStage: status === "running" ? "planning" : null,
  };
}

function userMessage(
  runValue: ResearchMessageRun,
  content = "このニュースの影響は？",
): ResearchUserMessage {
  return {
    role: "user",
    seq: 1,
    content,
    createdAt: "2026-07-13T00:00:00Z",
    run: runValue,
  };
}

function assistantMessage(
  content = "DBで確定した回答",
  details: Pick<ResearchAssistantMessage, "sources" | "missingAspects"> = {
    sources: [],
    missingAspects: [],
  },
): ResearchAssistantMessage {
  return {
    role: "assistant",
    seq: 2,
    content,
    createdAt: "2026-07-13T00:01:00Z",
    sources: details.sources,
    missingAspects: details.missingAspects,
  };
}

function thread(
  messages: ResearchThreadDetail["messages"],
  threadId = THREAD_ONE,
): ResearchThreadDetail {
  return { threadId, title: "AI市場調査", messages };
}

function activeThread(runId = RUN_ONE, threadId = THREAD_ONE) {
  return thread([userMessage(run(runId))], threadId);
}

function currentSource(): FakeEventSource {
  const source = FakeEventSource.instances.at(-1);
  if (source === undefined) throw new Error("expected EventSource");
  return source;
}

function latestScrollProps(): ScrollCapture {
  const props = mocks.scrollProps.at(-1);
  if (props === undefined) throw new Error("expected scroll props");
  return props;
}

function answerSlot(): HTMLElement {
  return screen.getByTestId("research-answer-slot");
}

function expectExclusiveAnswer(
  slot: HTMLElement,
  expected: "draft" | "final",
): void {
  const draft = within(slot).queryByText("一時下書き");
  const final = within(slot).queryByText("DBで確定した回答");
  expect([draft, final].filter((element) => element !== null)).toHaveLength(1);
  expect(draft !== null ? "draft" : "final").toBe(expected);
  expect(slot.textContent?.trim()).not.toBe("");
}

let animationFrameId = 0;
let animationFrames = new Map<number, FrameRequestCallback>();

function flushAnimationFrames(): void {
  const frames = [...animationFrames.values()];
  animationFrames = new Map();
  for (const callback of frames) callback(performance.now());
}

interface ConfiguredScroller {
  scrollTo: ReturnType<typeof vi.fn>;
  setScrollHeight: (value: number) => void;
}

function configureScroller(
  element: HTMLElement,
  geometry: {
    scrollHeight: number;
    clientHeight: number;
    scrollTop: number;
  },
): ConfiguredScroller {
  let scrollHeight = geometry.scrollHeight;
  Object.defineProperties(element, {
    scrollHeight: { configurable: true, get: () => scrollHeight },
    clientHeight: { configurable: true, value: geometry.clientHeight },
    scrollTop: {
      configurable: true,
      writable: true,
      value: geometry.scrollTop,
    },
  });
  const scrollTo = vi.fn((options: ScrollToOptions) => {
    element.scrollTop = Number(options.top ?? element.scrollTop);
  });
  Object.defineProperty(element, "scrollTo", {
    configurable: true,
    value: scrollTo,
  });
  return {
    scrollTo,
    setScrollHeight(value: number) {
      scrollHeight = value;
    },
  };
}

function visualAnswerAnchor(scroller: HTMLElement, documentTop: number): void {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      const text = this.textContent ?? "";
      const isAnswerAnchor =
        this.dataset.testid === "research-answer-slot" ||
        this.hasAttribute("data-research-answer-anchor") ||
        (this.tagName === "ARTICLE" &&
          (text.includes("一時下書き") || text.includes("DBで確定した回答")));
      const top = isAnswerAnchor ? documentTop - scroller.scrollTop : 0;
      const height = isAnswerAnchor ? 80 : 0;
      return {
        x: 0,
        y: top,
        top,
        right: 0,
        bottom: top + height,
        left: 0,
        width: 0,
        height,
        toJSON: () => ({}),
      };
    },
  );
}

function answerAnchor(text: string): HTMLElement {
  const content = screen.getByText(text);
  return (
    content.closest<HTMLElement>(
      '[data-testid="research-answer-slot"], [data-research-answer-anchor], article',
    ) ?? content
  );
}

function directChildContaining(
  parent: HTMLElement,
  descendant: HTMLElement,
): HTMLElement {
  const child = Array.from(parent.children).find((candidate) =>
    candidate.contains(descendant),
  );
  if (!(child instanceof HTMLElement)) {
    throw new Error("expected a direct child containing the element");
  }
  return child;
}

function onlyLiveAnnouncer(container: HTMLElement): HTMLElement {
  const owners = Array.from(
    container.querySelectorAll<HTMLElement>('[role="status"], [aria-live]'),
  );
  expect(owners).toHaveLength(1);
  const announcer = owners[0];
  if (announcer === undefined) throw new Error("live announcer is missing");
  expect(announcer).toHaveAttribute("role", "status");
  expect(announcer).toHaveAttribute("aria-live", "polite");
  expect(announcer).toHaveAttribute("aria-atomic", "true");
  expect(announcer).toHaveClass("sr-only");
  return announcer;
}

function onlyVisibleText(text: string): HTMLElement {
  const matches = screen
    .getAllByText(text)
    .filter((element) => element.closest(".sr-only") === null);
  expect(matches).toHaveLength(1);
  const match = matches[0];
  if (match === undefined) throw new Error("visible text is missing");
  return match;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

beforeEach(() => {
  mocks.refresh.mockReset();
  mocks.scrollProps.length = 0;
  mocks.renderActualScroll = false;
  FakeEventSource.instances.length = 0;
  animationFrameId = 0;
  animationFrames = new Map();
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => undefined)),
  );
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((callback: FrameRequestCallback) => {
      animationFrameId += 1;
      animationFrames.set(animationFrameId, callback);
      return animationFrameId;
    }),
  );
  vi.stubGlobal(
    "cancelAnimationFrame",
    vi.fn((id: number) => animationFrames.delete(id)),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ResearchThreadView live integration", () => {
  it("headerとcomposerをanswer scroller外の非scroll siblingに保つ", () => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const threadPane = view.container.querySelector("section");
    const header = view.container.querySelector("header");
    const composer = screen.getByTestId("composer-run-id");
    const answerScroller = latestScrollProps().containerRef.current;
    const answerRegion = answerScroller?.parentElement;

    expect(threadPane).not.toBeNull();
    expect(header?.parentElement).toBe(threadPane);
    expect(answerRegion?.parentElement).toBe(threadPane);
    expect(composer.parentElement).toBe(threadPane);
    expect(Array.from(threadPane?.children ?? [])).toEqual([
      header,
      answerRegion,
      composer,
    ]);
    expect(answerRegion).toHaveClass("min-h-0", "flex-1");
    expect(answerScroller).toHaveClass("h-full", "min-h-0", "overflow-y-auto");
  });

  it("promotes queued UI to running and shows the draft after accepted SSE", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            runId: RUN_ONE,
            threadId: THREAD_ONE,
            status: "queued",
            errorCode: null,
            progressStage: null,
            attemptEpoch: 0,
            recentEvents: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    render(
      <ResearchThreadView
        thread={thread([userMessage(run(RUN_ONE, "queued"))])}
      />,
    );
    expect(screen.getByText("待機中")).toBeInTheDocument();
    await act(async () => Promise.resolve());

    act(() => {
      currentSource().emit("attempt.started", { attemptEpoch: 1 }, "1-0");
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "queued後の下書き" },
        "2-0",
      );
    });

    expect(screen.queryByText("待機中")).not.toBeInTheDocument();
    expect(screen.getByText("queued後の下書き")).toBeInTheDocument();
    expect(screen.getByText("回答を生成中…")).toBeInTheDocument();
  });

  it("renders a safe question.resolved activity from the real polling response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            runId: RUN_ONE,
            threadId: THREAD_ONE,
            status: "running",
            errorCode: null,
            progressStage: "planning",
            attemptEpoch: 1,
            recentEvents: [
              {
                type: "question.resolved",
                ts: "2026-07-13T00:00:00Z",
                standaloneQuestion: "AI需要は伸びる？",
              },
              { type: "future.event", payload: "discarded" },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    render(<ResearchThreadView thread={activeThread()} />);

    expect(
      await screen.findByText("“AI需要は伸びる？”について調査中"),
    ).toBeInTheDocument();
    expect(screen.queryByText("discarded")).not.toBeInTheDocument();
  });

  it("places the status rail after the question-only bubble and before the temporary draft", () => {
    render(<ResearchThreadView thread={activeThread()} />);
    const userText = screen.getByText("このニュースの影響は？");
    const userArticle = userText.closest("article");
    const stage = screen.getByText("計画中");

    expect(userArticle).not.toBeNull();
    if (userArticle === null || userArticle.parentElement === null) {
      throw new Error("question turn is missing");
    }
    const statusRail = directChildContaining(userArticle.parentElement, stage);
    expect(userArticle).toHaveTextContent("このニュースの影響は？");
    expect(userArticle.textContent).toBe("このニュースの影響は？");
    expect(userArticle.nextElementSibling).toBe(statusRail);
    expect(screen.queryByText("回答を生成中…")).not.toBeInTheDocument();

    act(() => {
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 2, text: "保持suffixの下書き" },
        "1-0",
      );
    });

    const draft = screen.getByText("保持suffixの下書き");
    const draftArticle = draft.closest("article");
    expect(screen.getByText("回答を生成中…")).toBeInTheDocument();
    expect(draftArticle).not.toBeNull();
    expect(draftArticle).not.toBe(userArticle);
    expect(statusRail.nextElementSibling).toBe(draftArticle);
  });

  it.each([
    ["internal_error", "回答を生成できませんでした"],
    ["generation_unavailable", "回答を生成できませんでした"],
    ["stale", "時間切れになりました"],
    ["cancelled", "キャンセルしました"],
    ["enqueue_failed", "実行キューに投入できませんでした"],
  ] as const)("places persisted %s wording outside and immediately after the question bubble", (errorCode, message) => {
    const view = render(
      <ResearchThreadView
        thread={thread([userMessage(run(RUN_ONE, "failed", errorCode))])}
      />,
    );
    const question = screen.getByText("このニュースの影響は？");
    const questionArticle = question.closest("article");
    const status = screen.getByText(message);

    expect(questionArticle).not.toBeNull();
    if (questionArticle === null || questionArticle.parentElement === null) {
      throw new Error("question turn is missing");
    }
    const statusRail = directChildContaining(
      questionArticle.parentElement,
      status,
    );
    expect(questionArticle.textContent).toBe("このニュースの影響は？");
    expect(questionArticle.nextElementSibling).toBe(statusRail);
    expect(onlyVisibleText(message)).toBe(status);
    expect(status.closest('[role="status"], [aria-live]')).toBeNull();
    onlyLiveAnnouncer(view.container);
  });

  it("connects the scroll container and changes content revision only for answer content", () => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const questionArticle = screen
      .getByText("このニュースの影響は？")
      .closest("article");
    const initialScroll = latestScrollProps();
    expect(initialScroll.containerRef.current).not.toBeNull();
    expect(initialScroll.containerRef.current).toHaveClass("overflow-y-auto");

    act(() => {
      currentSource().emit(
        "stage",
        { attemptEpoch: 1, stage: "retrieving" },
        "1-0",
      );
    });
    const afterStage = latestScrollProps();
    expect(afterStage.contentRevision).toBe(initialScroll.contentRevision);
    expect(screen.getByText("このニュースの影響は？").closest("article")).toBe(
      questionArticle,
    );
    expect(questionArticle?.textContent).toBe("このニュースの影響は？");

    act(() => {
      currentSource().emit(
        "activity",
        {
          attemptEpoch: 1,
          activity: {
            type: "external_search.candidates_fetched",
            taskIndex: 0,
            candidateCount: 8,
          },
        },
        "2-0",
      );
    });
    const afterActivity = latestScrollProps();
    expect(afterActivity.contentRevision).toBe(afterStage.contentRevision);
    expect(screen.getByText("このニュースの影響は？").closest("article")).toBe(
      questionArticle,
    );
    expect(questionArticle?.textContent).toBe("このニュースの影響は？");

    act(() => {
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "draft" },
        "3-0",
      );
    });
    const afterDelta = latestScrollProps();
    expect(afterDelta.contentRevision).not.toBe(afterStage.contentRevision);

    act(() => {
      currentSource().emit(
        "answer.reset",
        { attemptEpoch: 1, generation: 2 },
        "4-0",
      );
    });
    const afterReset = latestScrollProps();
    expect(afterReset.contentRevision).not.toBe(afterDelta.contentRevision);

    view.rerender(
      <ResearchThreadView
        thread={thread([
          userMessage(run(RUN_ONE, "completed")),
          assistantMessage(),
        ])}
      />,
    );
    expect(latestScrollProps().contentRevision).not.toBe(
      afterReset.contentRevision,
    );
  });

  it("uses one stable announcer for queued, stage, generation, finalizing, and completion without announcing activity or draft text", () => {
    const view = render(
      <ResearchThreadView
        thread={thread([userMessage(run(RUN_ONE, "queued"))])}
      />,
    );
    const focusTarget = screen.getByRole("button", { name: "削除" });
    focusTarget.focus();
    const announcer = onlyLiveAnnouncer(view.container);
    expect(announcer).toHaveTextContent("待機中");

    act(() => {
      currentSource().emit("attempt.started", { attemptEpoch: 1 }, "1-0");
      currentSource().emit(
        "stage",
        { attemptEpoch: 1, stage: "retrieving" },
        "2-0",
      );
    });
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(announcer).toHaveTextContent("情報収集中");
    expect(focusTarget).toHaveFocus();

    act(() => {
      currentSource().emit(
        "activity",
        {
          attemptEpoch: 1,
          activity: {
            type: "external_search.candidates_fetched",
            taskIndex: 0,
            candidateCount: 8,
          },
        },
        "3-0",
      );
    });
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(announcer).toHaveTextContent("情報収集中");
    expect(announcer).not.toHaveTextContent("候補8件を取得");

    act(() => {
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "通知しない下書き本文" },
        "4-0",
      );
    });
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(announcer).toHaveTextContent("回答を生成中…");
    expect(announcer).not.toHaveTextContent("通知しない下書き本文");

    act(() => {
      currentSource().emit(
        "terminal",
        { attemptEpoch: 1, status: "completed" },
        "5-0",
      );
    });
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(announcer).toHaveTextContent("回答を確定しています…");

    view.rerender(
      <ResearchThreadView
        thread={thread([
          userMessage(run(RUN_ONE, "completed")),
          assistantMessage(),
        ])}
      />,
    );
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(announcer).toHaveTextContent("回答が完了しました");
    expect(focusTarget).toHaveFocus();
  });

  it.each([
    ["internal_error", "回答を生成できませんでした"],
    ["cancelled", "キャンセルしました"],
  ] as const)("announces a live %s terminal state from the single stable region", (errorCode, message) => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const announcer = onlyLiveAnnouncer(view.container);

    act(() => {
      currentSource().emit(
        "terminal",
        { attemptEpoch: 1, status: "failed", errorCode },
        "1-0",
      );
    });

    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(announcer).toHaveTextContent(message);
    expect(
      screen.getByText("このニュースの影響は？").closest("article"),
    ).not.toContainElement(screen.getByText(message));
  });

  it("keeps one EventSource and the same draft through CONNECTING retry before finalizing", () => {
    const fetchMock = vi.mocked(fetch);
    render(<ResearchThreadView thread={activeThread()} />);
    const source = currentSource();
    source.open();
    act(() => {
      source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "下書き" },
        "1-0",
      );
      source.reconnecting();
    });

    expect(screen.getByText("下書き")).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/research/runs/${RUN_ONE}`,
      expect.objectContaining({ cache: "no-store" }),
    );

    act(() => {
      source.emit("terminal", { attemptEpoch: 1, status: "completed" }, "2-0");
    });
    expect(screen.getByText("回答を確定しています…")).toBeInTheDocument();
    expect(screen.getByText("下書き")).toBeInTheDocument();
    expect(source.closeCount).toBe(1);
    expect(mocks.refresh).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["internal_error", "回答を生成できませんでした"],
    ["generation_unavailable", "回答を生成できませんでした"],
    ["stale", "時間切れになりました"],
    ["cancelled", "キャンセルしました"],
  ] as const)("removes draft and shows a fixed message for %s", (errorCode, message) => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const source = currentSource();
    const questionArticle = screen
      .getByText("このニュースの影響は？")
      .closest("article");
    expect(questionArticle).not.toBeNull();
    if (questionArticle === null || questionArticle.parentElement === null) {
      throw new Error("question turn is missing");
    }
    act(() => {
      source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "消える下書き" },
        "1-0",
      );
      source.emit(
        "terminal",
        {
          attemptEpoch: 1,
          status: "failed",
          errorCode,
          internalDetail: "INTERNAL_PAYLOAD_SHOULD_NOT_LEAK",
        },
        "2-0",
      );
    });

    expect(screen.queryByText("消える下書き")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("research-answer-slot"),
    ).not.toBeInTheDocument();
    const failure = onlyVisibleText(message);
    const statusRail = directChildContaining(
      questionArticle.parentElement,
      failure,
    );
    expect(questionArticle.textContent).toBe("このニュースの影響は？");
    expect(questionArticle.nextElementSibling).toBe(statusRail);
    expect(failure.closest('[role="status"], [aria-live]')).toBeNull();
    expect(onlyLiveAnnouncer(view.container)).toHaveTextContent(message);
    expect(
      screen.queryByText("INTERNAL_PAYLOAD_SHOULD_NOT_LEAK"),
    ).not.toBeInTheDocument();
  });

  it("keeps one failed turn, failure rail, focus, and announcement across live-to-persisted convergence", () => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const source = currentSource();
    const questionArticle = screen
      .getByText("このニュースの影響は？")
      .closest("article");
    expect(questionArticle).not.toBeNull();
    if (questionArticle === null || questionArticle.parentElement === null) {
      throw new Error("question turn is missing");
    }
    const turn = questionArticle.parentElement;
    expect(turn).toHaveAttribute("data-research-run-id", RUN_ONE);
    expect(turn).toHaveAttribute("data-research-persisted-status", "running");
    expect(turn.querySelectorAll("[data-research-failure-rail]")).toHaveLength(
      0,
    );
    const answerScrollRegion = latestScrollProps().containerRef.current;
    expect(answerScrollRegion).not.toBeNull();
    if (answerScrollRegion === null) {
      throw new Error("answer scroll region is missing");
    }
    expect(answerScrollRegion).toHaveAttribute(
      "data-research-answer-scroll-region",
    );
    const focusTarget = screen.getByRole("button", { name: "削除" });
    focusTarget.focus();
    const announcer = onlyLiveAnnouncer(view.container);

    act(() => {
      source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "失敗前の下書き" },
        "1-0",
      );
    });
    expect(screen.getByText("失敗前の下書き")).toBeInTheDocument();
    expect(answerSlot()).toContainElement(screen.getByText("失敗前の下書き"));
    expect(turn).toHaveAttribute("data-research-persisted-status", "running");
    expect(turn.querySelectorAll("[data-research-failure-rail]")).toHaveLength(
      0,
    );
    expect(latestScrollProps().containerRef.current).toBe(answerScrollRegion);
    const revisionBeforeFailure =
      latestScrollProps().failedContractionRevision ?? 0;

    act(() => {
      source.emit(
        "terminal",
        {
          attemptEpoch: 1,
          status: "failed",
          errorCode: "internal_error",
        },
        "2-0",
      );
    });

    expect(screen.queryByText("失敗前の下書き")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("research-answer-slot"),
    ).not.toBeInTheDocument();
    const liveFailure = onlyVisibleText("回答を生成できませんでした");
    const liveFailureRail = directChildContaining(turn, liveFailure);
    const liveFailureRails = turn.querySelectorAll(
      "[data-research-failure-rail]",
    );
    expect(turn).toHaveAttribute("data-research-run-id", RUN_ONE);
    expect(turn).toHaveAttribute("data-research-persisted-status", "running");
    expect(liveFailureRails).toHaveLength(1);
    expect(liveFailureRails[0]).toBe(liveFailureRail);
    expect(latestScrollProps().containerRef.current).toBe(answerScrollRegion);
    expect(questionArticle.textContent).toBe("このニュースの影響は？");
    expect(questionArticle.nextElementSibling).toBe(liveFailureRail);
    expect(liveFailure.closest('[role="status"], [aria-live]')).toBeNull();
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(announcer).toHaveTextContent("回答を生成できませんでした");
    expect(focusTarget).toHaveFocus();
    const liveFailureRevision =
      latestScrollProps().failedContractionRevision ?? 0;
    expect(liveFailureRevision).toBeGreaterThan(revisionBeforeFailure);
    const announcementNode = announcer.firstChild;
    const announcementMutations: MutationRecord[] = [];
    const observer = new MutationObserver((records) => {
      announcementMutations.push(...records);
    });
    observer.observe(announcer, {
      characterData: true,
      childList: true,
      subtree: true,
    });

    view.rerender(
      <ResearchThreadView
        thread={thread([userMessage(run(RUN_ONE, "failed", "internal_error"))])}
      />,
    );
    announcementMutations.push(...observer.takeRecords());
    observer.disconnect();

    const persistedQuestionArticle = screen
      .getByText("このニュースの影響は？")
      .closest("article");
    const persistedFailure = onlyVisibleText("回答を生成できませんでした");
    const persistedFailureRail = directChildContaining(turn, persistedFailure);
    const persistedFailureRails = turn.querySelectorAll(
      "[data-research-failure-rail]",
    );
    expect(persistedQuestionArticle).toBe(questionArticle);
    expect(questionArticle.parentElement).toBe(turn);
    expect(turn).toHaveAttribute("data-research-run-id", RUN_ONE);
    expect(turn).toHaveAttribute("data-research-persisted-status", "failed");
    expect(persistedFailure).toBe(liveFailure);
    expect(persistedFailureRail).toBe(liveFailureRail);
    expect(persistedFailureRails).toHaveLength(1);
    expect(persistedFailureRails[0]).toBe(liveFailureRail);
    expect(latestScrollProps().containerRef.current).toBe(answerScrollRegion);
    expect(answerScrollRegion).toHaveAttribute(
      "data-research-answer-scroll-region",
    );
    expect(questionArticle.nextElementSibling).toBe(persistedFailureRail);
    expect(
      screen.queryByTestId("research-answer-slot"),
    ).not.toBeInTheDocument();
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(announcer.firstChild).toBe(announcementNode);
    expect(announcementMutations).toHaveLength(0);
    expect(focusTarget).toHaveFocus();
    expect(latestScrollProps().failedContractionRevision ?? 0).toBe(
      liveFailureRevision,
    );
  });

  it("uses the same question-adjacent failure rail when polling observes failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "failed",
            progressStage: "synthesizing",
            attemptEpoch: 1,
            recentEvents: [],
            errorCode: "stale",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const questionArticle = screen
      .getByText("このニュースの影響は？")
      .closest("article");
    expect(questionArticle).not.toBeNull();
    if (questionArticle === null || questionArticle.parentElement === null) {
      throw new Error("question turn is missing");
    }

    await waitFor(() => {
      expect(
        screen.queryByTestId("research-answer-slot"),
      ).not.toBeInTheDocument();
      expect(
        screen
          .getAllByText("時間切れになりました")
          .filter((element) => element.closest(".sr-only") === null),
      ).toHaveLength(1);
    });

    const failure = onlyVisibleText("時間切れになりました");
    const statusRail = directChildContaining(
      questionArticle.parentElement,
      failure,
    );
    expect(questionArticle.textContent).toBe("このニュースの影響は？");
    expect(questionArticle.nextElementSibling).toBe(statusRail);
    expect(failure.closest('[role="status"], [aria-live]')).toBeNull();
    expect(onlyLiveAnnouncer(view.container)).toHaveTextContent(
      "時間切れになりました",
    );
  });

  it("S4B CLOSEDでは同じanswer slotで未完了draftをplaceholderへ抑制し、poll完了後にDB回答へ収束する", async () => {
    const pendingResponse = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => pendingResponse.promise),
    );
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const source = currentSource();
    const focusTarget = screen.getByRole("button", { name: "削除" });
    focusTarget.focus();
    act(() => {
      source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "不完全な下書き" },
        "1-0",
      );
    });
    expect(screen.getByText("不完全な下書き")).toBeInTheDocument();
    const slot = answerSlot();
    expect(slot).toContainElement(screen.getByText("不完全な下書き"));
    expect(slot.textContent?.trim().length).toBeGreaterThan(0);
    expect(focusTarget).toHaveFocus();

    act(() => source.closed());
    expect(answerSlot()).toBe(slot);
    expect(screen.queryByText("不完全な下書き")).not.toBeInTheDocument();
    expect(within(slot).queryByText("DBで確定した回答")).toBeNull();
    expect(slot.textContent?.trim().length).toBeGreaterThan(0);
    expect(slot).not.toHaveTextContent("回答を確定しています…");
    expect(slot).not.toHaveTextContent("回答が完了しました");
    expect(focusTarget).toHaveFocus();
    expect(FakeEventSource.instances).toHaveLength(1);

    await act(async () => {
      pendingResponse.resolve(
        new Response(
          JSON.stringify({
            runId: RUN_ONE,
            threadId: THREAD_ONE,
            status: "completed",
            errorCode: null,
            progressStage: null,
            attemptEpoch: 1,
            recentEvents: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
      await pendingResponse.promise;
      await Promise.resolve();
    });

    expect(answerSlot()).toBe(slot);
    expect(screen.getAllByText("回答を確定しています…").length).toBeGreaterThan(
      0,
    );
    expect(screen.queryByText("不完全な下書き")).not.toBeInTheDocument();
    expect(within(slot).queryByText("DBで確定した回答")).toBeNull();
    expect(slot.textContent?.trim().length).toBeGreaterThan(0);
    expect(focusTarget).toHaveFocus();
    expect(FakeEventSource.instances).toHaveLength(1);

    view.rerender(
      <ResearchThreadView
        thread={thread([
          userMessage(run(RUN_ONE, "completed")),
          assistantMessage(),
        ])}
      />,
    );

    expect(answerSlot()).toBe(slot);
    expectExclusiveAnswer(slot, "final");
    expect(screen.queryByText("不完全な下書き")).not.toBeInTheDocument();
    expect(focusTarget).toHaveFocus();
  });

  it("S4B 通常完了では同じanswer slotがdraftとfinalizingを保持し、空renderなしでDB回答だけへ置換する", () => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const focusTarget = screen.getByRole("button", { name: "削除" });
    focusTarget.focus();
    act(() => {
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "一時下書き" },
        "1-0",
      );
    });
    expect(screen.getByText("一時下書き")).toBeInTheDocument();
    const slot = answerSlot();
    expectExclusiveAnswer(slot, "draft");
    expect(focusTarget).toHaveFocus();

    act(() => {
      currentSource().emit(
        "terminal",
        { attemptEpoch: 1, status: "completed" },
        "2-0",
      );
    });

    expect(answerSlot()).toBe(slot);
    expectExclusiveAnswer(slot, "draft");
    expect(screen.getAllByText("回答を確定しています…").length).toBeGreaterThan(
      0,
    );
    expect(focusTarget).toHaveFocus();

    view.rerender(
      <ResearchThreadView
        thread={thread([
          userMessage(run(RUN_ONE, "completed")),
          assistantMessage(),
        ])}
      />,
    );

    expect(answerSlot()).toBe(slot);
    expectExclusiveAnswer(slot, "final");
    expect(screen.queryByText("一時下書き")).not.toBeInTheDocument();
    expect(screen.getByText("DBで確定した回答")).toBeInTheDocument();
    expect(screen.getByText("回答が完了しました")).toBeInTheDocument();
    expect(screen.queryByText("回答を生成中…")).not.toBeInTheDocument();
    expect(screen.queryByText("回答を確定しています…")).not.toBeInTheDocument();
    expect(focusTarget).toHaveFocus();
  });

  it("S4B 96px超でfinalを置換してもscrollTopを維持し、viewport外の回答にはlatest buttonを出す", () => {
    mocks.renderActualScroll = true;
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const scroller = latestScrollProps().containerRef.current;
    expect(scroller).not.toBeNull();
    if (scroller === null) throw new Error("answer scroller is missing");
    const configured = configureScroller(scroller, {
      scrollHeight: 1200,
      clientHeight: 500,
      scrollTop: 660,
    });
    visualAnswerAnchor(scroller, 1100);
    act(flushAnimationFrames);

    act(() => {
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "一時下書き" },
        "1-0",
      );
    });
    act(flushAnimationFrames);
    scroller.scrollTop = 400;
    act(() => scroller.dispatchEvent(new Event("scroll")));
    configured.scrollTo.mockClear();
    const scrollTopBeforeFinal = scroller.scrollTop;
    const focusTarget = screen.getByRole("button", { name: "削除" });
    focusTarget.focus();
    configured.setScrollHeight(1500);

    view.rerender(
      <ResearchThreadView
        thread={thread([
          userMessage(run(RUN_ONE, "completed")),
          assistantMessage(),
        ])}
      />,
    );
    act(flushAnimationFrames);

    const finalAnchor = answerAnchor("DBで確定した回答");
    expect(
      Math.abs(scroller.scrollTop - scrollTopBeforeFinal),
    ).toBeLessThanOrEqual(1);
    expect(configured.scrollTo).not.toHaveBeenCalled();
    expect(finalAnchor.getBoundingClientRect().top).toBeGreaterThan(
      scroller.clientHeight,
    );
    expect(
      screen.getByRole("button", { name: "最新の回答へ" }),
    ).toBeInTheDocument();
    expect(focusTarget).toHaveFocus();
  });

  it("S4B 末尾付近のfinal・source・missing同時commitでもanswer anchorを保ち絶対末尾へscrollしない", () => {
    mocks.renderActualScroll = true;
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const scroller = latestScrollProps().containerRef.current;
    expect(scroller).not.toBeNull();
    if (scroller === null) throw new Error("answer scroller is missing");
    const configured = configureScroller(scroller, {
      scrollHeight: 1200,
      clientHeight: 500,
      scrollTop: 660,
    });
    visualAnswerAnchor(scroller, 800);
    act(flushAnimationFrames);

    act(() => {
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "一時下書き" },
        "1-0",
      );
    });
    act(flushAnimationFrames);
    scroller.scrollTop = 660;
    act(() => scroller.dispatchEvent(new Event("scroll")));
    configured.scrollTo.mockClear();
    const draftAnchor = answerAnchor("一時下書き");
    const anchorTopBeforeFinal = draftAnchor.getBoundingClientRect().top;
    const focusTarget = screen.getByRole("button", { name: "削除" });
    focusTarget.focus();
    configured.setScrollHeight(1600);

    view.rerender(
      <ResearchThreadView
        thread={thread([
          userMessage(run(RUN_ONE, "completed")),
          assistantMessage("DBで確定した回答 [[1]]", {
            sources: [
              {
                kind: "external_url",
                sourceRef: "1",
                url: "https://example.com/final-source",
                title: "確定ソース",
                sourceName: "Example",
                publishedAt: "2026-07-13T00:00:00Z",
                evidenceClaim: "確定回答を裏付ける情報",
              },
            ],
            missingAspects: ["追加確認が必要な論点"],
          }),
        ])}
      />,
    );
    act(flushAnimationFrames);

    const finalAnchor = answerAnchor("DBで確定した回答");
    const anchorTopAfterFinal = finalAnchor.getBoundingClientRect().top;
    expect(
      Math.abs(anchorTopAfterFinal - anchorTopBeforeFinal),
    ).toBeLessThanOrEqual(1);
    expect(configured.scrollTo).not.toHaveBeenCalledWith({
      top: scroller.scrollHeight,
      behavior: "auto",
    });
    expect(screen.getByRole("button", { name: "出典 1" })).toBeInTheDocument();
    expect(screen.getByText("追加確認が必要な論点")).toBeInTheDocument();
    expect(focusTarget).toHaveFocus();
  });

  it("renders final missing aspects inside the answer as a labeled semantic list", () => {
    render(
      <ResearchThreadView
        thread={thread([
          userMessage(run(RUN_ONE, "completed")),
          assistantMessage("確定した回答", {
            sources: [],
            missingAspects: ["企業の一次情報", "地域別の内訳"],
          }),
        ])}
      />,
    );

    const slot = answerSlot();
    const label = within(slot).getByText("確認できなかった点");
    const list = within(slot).getByRole("list");
    expect(label).toBeVisible();
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["企業の一次情報", "地域別の内訳"]);
    expect(slot).not.toHaveTextContent("企業の一次情報 / 地域別の内訳");
  });

  it("cleans the old run before subscribing to another thread and ignores its late event", () => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
    const oldSource = currentSource();
    act(() => {
      oldSource.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "旧run下書き" },
        "1-0",
      );
    });

    view.rerender(
      <ResearchThreadView thread={activeThread(RUN_TWO, THREAD_TWO)} />,
    );
    const newSource = currentSource();
    expect(newSource).not.toBe(oldSource);
    expect(oldSource.closeCount).toBe(1);
    expect(screen.queryByText("旧run下書き")).not.toBeInTheDocument();

    act(() => {
      oldSource.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "遅延した旧run" },
        "2-0",
      );
      newSource.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "新run下書き" },
        "1-0",
      );
    });

    expect(screen.queryByText("遅延した旧run")).not.toBeInTheDocument();
    expect(screen.getByText("新run下書き")).toBeInTheDocument();
    expect(screen.getByTestId("composer-run-id")).toHaveTextContent(RUN_TWO);
  });
});
