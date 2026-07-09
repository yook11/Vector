import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ActiveRunStatus } from "./ActiveRunStatus";

const mocks = vi.hoisted(() => {
  const refresh = vi.fn();
  return {
    refresh,
    router: { refresh },
  };
});

vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
}));

const RUN_ID = "00000000-0000-4000-a000-000000000010";

let hiddenSpy: ReturnType<typeof vi.spyOn>;

function runResponse(
  status: "queued" | "running" | "completed" | "failed",
  progressStage: "planning" | "retrieving" | "synthesizing" | null,
  recentEvents: unknown[] = [],
) {
  return new Response(
    JSON.stringify({
      runId: RUN_ID,
      threadId: "00000000-0000-4000-a000-000000000020",
      status,
      errorCode: status === "failed" ? "internal_error" : null,
      progressStage,
      recentEvents,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  hiddenSpy = vi.spyOn(document, "hidden", "get").mockReturnValue(false);
});

afterEach(() => {
  hiddenSpy.mockRestore();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("ActiveRunStatus", () => {
  it("renders queued initial status before the first poll completes", () => {
    const fetchMock = vi.fn(() => new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="queued"
        initialStage={null}
      />,
    );

    expect(screen.getByText("待機中")).toBeInTheDocument();
  });

  it("renders running null-stage fallback before the first poll completes", () => {
    const fetchMock = vi.fn(() => new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage={null}
      />,
    );

    expect(screen.getByText("生成中")).toBeInTheDocument();
  });

  it("updates stage text without refreshing the thread", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(runResponse("running", "retrieving"));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage="planning"
      />,
    );

    expect(screen.getByText("計画中")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("情報収集中")).toBeInTheDocument(),
    );
    expect(mocks.refresh).not.toHaveBeenCalled();
  });

  it("shows latest known live event subtext while retrieving", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      runResponse("running", "retrieving", [
        {
          type: "external_search.queries_generated",
          ts: "2026-07-09T01:00:00Z",
          taskIndex: 0,
          queries: ["NVIDIA AI"],
        },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage="planning"
      />,
    );

    await waitFor(() =>
      expect(screen.getByText("情報収集中")).toBeInTheDocument(),
    );
    expect(screen.getByText("“NVIDIA AI” を検索中")).toBeInTheDocument();
    expect(mocks.refresh).not.toHaveBeenCalled();
  });

  it("shows the resolved question while planning", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      runResponse("running", "planning", [
        {
          type: "question.resolved",
          ts: "2026-07-09T01:00:00Z",
          standaloneQuestion: "NVIDIA の発表が株価へ与える影響は？",
        },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage={null}
      />,
    );

    await waitFor(() => expect(screen.getByText("計画中")).toBeInTheDocument());
    expect(
      screen.getByText("“NVIDIA の発表が株価へ与える影響は？”について調査中"),
    ).toBeInTheDocument();
  });

  it("shows the resolved question before planning starts", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      runResponse("running", null, [
        {
          type: "question.resolved",
          ts: "2026-07-09T01:00:00Z",
          standaloneQuestion: "NVIDIA の発表が株価へ与える影響は？",
        },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage={null}
      />,
    );

    await waitFor(() => expect(screen.getByText("生成中")).toBeInTheDocument());
    expect(
      screen.getByText("“NVIDIA の発表が株価へ与える影響は？”について調査中"),
    ).toBeInTheDocument();
  });

  it("prioritizes search status over the resolved question while retrieving", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      runResponse("running", "retrieving", [
        {
          type: "question.resolved",
          ts: "2026-07-09T01:00:00Z",
          standaloneQuestion: "NVIDIA の発表が株価へ与える影響は？",
        },
        {
          type: "external_search.queries_generated",
          ts: "2026-07-09T01:00:01Z",
          taskIndex: 0,
          queries: ["NVIDIA stock impact"],
        },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage="planning"
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByText("“NVIDIA stock impact” を検索中"),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByText("“NVIDIA の発表が株価へ与える影響は？”について調査中"),
    ).not.toBeInTheDocument();
  });

  it("uses the most recent known event when newer events are unknown", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      runResponse("running", "retrieving", [
        {
          type: "external_search.candidates_fetched",
          ts: "2026-07-09T01:00:00Z",
          taskIndex: 0,
          candidateCount: 8,
        },
        {
          type: "future.event",
          ts: "2026-07-09T01:00:01Z",
        },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage="planning"
      />,
    );

    await waitFor(() =>
      expect(screen.getByText("候補8件を取得")).toBeInTheDocument(),
    );
  });

  it("hides live event subtext outside retrieving", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      runResponse("running", "synthesizing", [
        {
          type: "external_search.candidates_fetched",
          ts: "2026-07-09T01:00:00Z",
          taskIndex: 0,
          candidateCount: 8,
        },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage="retrieving"
      />,
    );

    await waitFor(() =>
      expect(screen.getByText("回答作成中")).toBeInTheDocument(),
    );
    expect(screen.queryByText("候補8件を取得")).not.toBeInTheDocument();
  });

  it("refreshes on terminal status", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(runResponse("completed", "synthesizing"));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage="synthesizing"
      />,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledTimes(1));
  });

  it("pauses while hidden and polls immediately when visible again", async () => {
    hiddenSpy.mockReturnValue(true);
    const fetchMock = vi
      .fn()
      .mockResolvedValue(runResponse("running", "planning"));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage={null}
      />,
    );

    expect(fetchMock).not.toHaveBeenCalled();

    hiddenSpy.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("backs off after transient poll failures", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 500 }))
      .mockResolvedValueOnce(runResponse("running", "planning"));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage={null}
      />,
    );

    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3999);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("計画中")).toBeInTheDocument();
  });

  it("refreshes and stops on 404", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ActiveRunStatus
        runId={RUN_ID}
        initialStatus="running"
        initialStage={null}
      />,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledTimes(1));
  });
});
