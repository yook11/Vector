import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/error";
import type { ResearchRunResponse } from "@/types/types.gen";

const mocks = vi.hoisted(() => ({
  getResearchRun: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("@/features/research", () => ({
  getResearchRun: mocks.getResearchRun,
  ResearchUuidSchema: {
    safeParse: (value: string) =>
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
        value,
      )
        ? { success: true, data: value }
        : { success: false },
  },
}));

import { GET } from "./route";

const RUN_ID = "00000000-0000-4000-a000-000000000010";
const THREAD_ID = "00000000-0000-4000-a000-000000000020";

function context(runId: string) {
  return { params: Promise.resolve({ runId }) };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("GET /api/research/runs/[runId]", () => {
  it("returns 400 with no-store for malformed run ids", async () => {
    const response = await GET(
      new Request("http://test.local"),
      context("bad-id"),
    );

    expect(response.status).toBe(400);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(mocks.getResearchRun).not.toHaveBeenCalled();
  });

  it("proxies the slim run signal with no-store", async () => {
    const data: ResearchRunResponse = {
      runId: RUN_ID,
      threadId: THREAD_ID,
      status: "running",
      errorCode: null,
    };
    mocks.getResearchRun.mockResolvedValue(data);

    const response = await GET(
      new Request("http://test.local"),
      context(RUN_ID),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual(data);
    expect(mocks.getResearchRun).toHaveBeenCalledWith(RUN_ID);
  });

  it("passes through ownership and missing-resource statuses with no-store", async () => {
    mocks.getResearchRun.mockRejectedValue(new ApiError(404, "Not Found"));

    const response = await GET(
      new Request("http://test.local"),
      context(RUN_ID),
    );

    expect(response.status).toBe(404);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual({ error: "Not Found" });
  });
});
