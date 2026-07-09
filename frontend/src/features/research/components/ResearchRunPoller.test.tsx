import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResearchRunPoller } from "./ResearchRunPoller";

const mocks = vi.hoisted(() => ({
  refresh: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: mocks.refresh }),
}));

const RUN_ID = "00000000-0000-4000-a000-000000000010";
let hiddenSpy: ReturnType<typeof vi.spyOn>;

function runResponse(status: "queued" | "running" | "completed" | "failed") {
  return new Response(
    JSON.stringify({
      runId: RUN_ID,
      threadId: "00000000-0000-4000-a000-000000000020",
      status,
      errorCode: status === "failed" ? "internal_error" : null,
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
});

describe("ResearchRunPoller", () => {
  it("refreshes on terminal status", async () => {
    const fetchMock = vi.fn().mockResolvedValue(runResponse("completed"));
    vi.stubGlobal("fetch", fetchMock);

    render(<ResearchRunPoller runId={RUN_ID} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledTimes(1));
  });

  it("pauses while hidden and polls immediately when visible again", async () => {
    hiddenSpy.mockReturnValue(true);
    const fetchMock = vi.fn().mockResolvedValue(runResponse("running"));
    vi.stubGlobal("fetch", fetchMock);

    render(<ResearchRunPoller runId={RUN_ID} />);

    expect(fetchMock).not.toHaveBeenCalled();

    hiddenSpy.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("refreshes and stops on 404", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<ResearchRunPoller runId={RUN_ID} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledTimes(1));
  });
});
