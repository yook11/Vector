import { describe, expect, it, vi } from "vitest";
import type {
  PaginatedResearchThreadResponse,
  ResearchRunResponse,
  ResearchThreadDetail,
} from "@/types/types.gen";

vi.mock("@/lib/api/hey-api-interceptors", () => ({}));
vi.mock("@/types/sdk.gen", () => ({
  getResearchRun: vi.fn(),
  getResearchThread: vi.fn(),
  listResearchThreads: vi.fn(),
}));

import { getResearchRun } from "./get-research-run";
import { getResearchThread } from "./get-research-thread";
import { getResearchThreads } from "./get-research-threads";

const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const RUN_ID = "00000000-0000-4000-a000-000000000002";

describe("Research read cache contract", () => {
  it("thread一覧をno-storeで取得する", async () => {
    const data = {
      items: [],
      total: 0,
      page: 1,
      perPage: 20,
      totalPages: 0,
    } satisfies PaginatedResearchThreadResponse;
    const fetcher = vi.fn().mockResolvedValue({ data });

    await expect(
      getResearchThreads(
        20,
        fetcher as unknown as NonNullable<
          Parameters<typeof getResearchThreads>[1]
        >,
      ),
    ).resolves.toBe(data);

    expect(fetcher).toHaveBeenCalledWith({
      throwOnError: true,
      cache: "no-store",
      query: { page: 1, perPage: 20 },
    });
  });

  it("thread詳細をno-storeで取得する", async () => {
    const data = {
      threadId: THREAD_ID,
      title: "Research thread",
      messages: [],
    } satisfies ResearchThreadDetail;
    const fetcher = vi.fn().mockResolvedValue({ data });

    await expect(
      getResearchThread(
        THREAD_ID,
        fetcher as unknown as NonNullable<
          Parameters<typeof getResearchThread>[1]
        >,
      ),
    ).resolves.toBe(data);

    expect(fetcher).toHaveBeenCalledWith({
      throwOnError: true,
      cache: "no-store",
      path: { thread_id: THREAD_ID },
    });
  });

  it("run状態をno-storeで取得する", async () => {
    const data = {
      runId: RUN_ID,
      threadId: THREAD_ID,
      status: "running",
      errorCode: null,
      progressStage: "synthesizing",
      attemptEpoch: 1,
      recentEvents: [],
    } satisfies ResearchRunResponse;
    const fetcher = vi.fn().mockResolvedValue({ data });

    await expect(
      getResearchRun(
        RUN_ID,
        fetcher as unknown as NonNullable<Parameters<typeof getResearchRun>[1]>,
      ),
    ).resolves.toBe(data);

    expect(fetcher).toHaveBeenCalledWith({
      throwOnError: true,
      cache: "no-store",
      path: { run_id: RUN_ID },
    });
  });
});
