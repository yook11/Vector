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
) {
  return new Response(
    JSON.stringify({
      runId: RUN_ID,
      threadId: "00000000-0000-4000-a000-000000000020",
      status,
      errorCode: status === "failed" ? "internal_error" : null,
      progressStage,
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
