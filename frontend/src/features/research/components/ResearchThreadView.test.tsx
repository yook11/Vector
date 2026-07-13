import { act, render, screen } from "@testing-library/react";
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
}

const mocks = vi.hoisted(() => ({
  refresh: vi.fn(),
  scrollProps: [] as ScrollCapture[],
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

vi.mock("./ResearchLiveScrollButton", () => ({
  ResearchLiveScrollButton: (props: ScrollCapture) => {
    mocks.scrollProps.push(props);
    return null;
  },
}));

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
): ResearchAssistantMessage {
  return {
    role: "assistant",
    seq: 2,
    content,
    createdAt: "2026-07-13T00:01:00Z",
    sources: [],
    missingAspects: [],
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
  FakeEventSource.instances.length = 0;
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => undefined)),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ResearchThreadView live integration", () => {
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

  it("keeps progress in the user card and adds the first draft in a following assistant article", () => {
    render(<ResearchThreadView thread={activeThread()} />);
    const userText = screen.getByText("このニュースの影響は？");
    const userArticle = userText.closest("article");

    expect(screen.getByText("計画中")).toBeInTheDocument();
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
    expect(userArticle).not.toBeNull();
    expect(draftArticle).not.toBeNull();
    expect(draftArticle).not.toBe(userArticle);
    expect(userArticle?.nextElementSibling).toBe(draftArticle);
  });

  it("connects the scroll container and changes content revision only for answer content", () => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
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

    act(() => {
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "draft" },
        "2-0",
      );
    });
    const afterDelta = latestScrollProps();
    expect(afterDelta.contentRevision).not.toBe(afterStage.contentRevision);

    act(() => {
      currentSource().emit(
        "answer.reset",
        { attemptEpoch: 1, generation: 2 },
        "3-0",
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
    ["cancelled", "キャンセルしました"],
  ] as const)("removes draft and shows a fixed message for %s", (errorCode, message) => {
    render(<ResearchThreadView thread={activeThread()} />);
    const source = currentSource();
    act(() => {
      source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "消える下書き" },
        "1-0",
      );
      source.emit(
        "terminal",
        { attemptEpoch: 1, status: "failed", errorCode },
        "2-0",
      );
    });

    expect(screen.queryByText("消える下書き")).not.toBeInTheDocument();
    expect(screen.getByText(message)).toBeInTheDocument();
  });

  it("removes a visible draft on CLOSED and never revives it after polling completion", async () => {
    const pendingResponse = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => pendingResponse.promise),
    );
    render(<ResearchThreadView thread={activeThread()} />);
    const source = currentSource();
    act(() => {
      source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "不完全な下書き" },
        "1-0",
      );
    });
    expect(screen.getByText("不完全な下書き")).toBeInTheDocument();

    act(() => source.closed());
    expect(screen.queryByText("不完全な下書き")).not.toBeInTheDocument();
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
            recentEvents: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
      await pendingResponse.promise;
      await Promise.resolve();
    });

    expect(screen.getByText("回答を確定しています…")).toBeInTheDocument();
    expect(screen.queryByText("不完全な下書き")).not.toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("atomically replaces a live draft with the DB assistant message", () => {
    const view = render(<ResearchThreadView thread={activeThread()} />);
    act(() => {
      currentSource().emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "一時下書き" },
        "1-0",
      );
    });
    expect(screen.getByText("一時下書き")).toBeInTheDocument();

    view.rerender(
      <ResearchThreadView
        thread={thread([
          userMessage(run(RUN_ONE, "completed")),
          assistantMessage(),
        ])}
      />,
    );

    expect(screen.queryByText("一時下書き")).not.toBeInTheDocument();
    expect(screen.getByText("DBで確定した回答")).toBeInTheDocument();
    expect(screen.getByText("回答が完了しました")).toBeInTheDocument();
    expect(screen.queryByText("回答を生成中…")).not.toBeInTheDocument();
    expect(screen.queryByText("回答を確定しています…")).not.toBeInTheDocument();
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
