import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/error";
import type {
  PaginatedResearchThreadResponse,
  ResearchThreadDetail,
} from "@/types/types.gen";
import { loadResearchThreadPage } from "./research-thread";

const mocks = vi.hoisted(() => ({
  getResearchThread: vi.fn(),
  getResearchThreads: vi.fn(),
}));

vi.mock("../api/get-research-thread", () => ({
  getResearchThread: mocks.getResearchThread,
}));

vi.mock("../api/get-research-threads", () => ({
  getResearchThreads: mocks.getResearchThreads,
}));

const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const thread: ResearchThreadDetail = {
  threadId: THREAD_ID,
  title: "Thread A",
  messages: [],
};
const threads = {
  items: [],
  total: 0,
  page: 1,
  perPage: 20,
  totalPages: 0,
} as unknown as PaginatedResearchThreadResponse;

beforeEach(() => {
  mocks.getResearchThread.mockReset();
  mocks.getResearchThreads.mockReset();
});

describe("loadResearchThreadPage", () => {
  it("detailとlistを同時に開始して両成功時だけreadyを返す", async () => {
    let resolveDetail!: (value: ResearchThreadDetail) => void;
    let resolveList!: (value: PaginatedResearchThreadResponse) => void;
    mocks.getResearchThread.mockImplementation(
      () =>
        new Promise<ResearchThreadDetail>((resolve) => {
          resolveDetail = resolve;
        }),
    );
    mocks.getResearchThreads.mockImplementation(
      () =>
        new Promise<PaginatedResearchThreadResponse>((resolve) => {
          resolveList = resolve;
        }),
    );

    const resultPromise = loadResearchThreadPage(THREAD_ID, 20);
    expect(mocks.getResearchThread).toHaveBeenCalledWith(THREAD_ID);
    expect(mocks.getResearchThreads).toHaveBeenCalledWith(20);

    resolveList(threads);
    resolveDetail(thread);
    await expect(resultPromise).resolves.toEqual({
      state: "ready",
      thread,
      threads,
    });
  });

  it("detail 404とlist failureが同時でもnot-foundを優先する", async () => {
    mocks.getResearchThread.mockRejectedValue(new ApiError(404, "Not found"));
    mocks.getResearchThreads.mockRejectedValue(new Error("list failed"));

    await expect(loadResearchThreadPage(THREAD_ID, 20)).resolves.toEqual({
      state: "not-found",
    });
  });

  it("detailの非404 errorをlist errorより優先する", async () => {
    const detailError = new ApiError(503, "detail failed");
    mocks.getResearchThread.mockRejectedValue(detailError);
    mocks.getResearchThreads.mockRejectedValue(new Error("list failed"));

    await expect(loadResearchThreadPage(THREAD_ID, 20)).rejects.toBe(
      detailError,
    );
  });

  it("detail成功時はlist errorをthrowする", async () => {
    const listError = new Error("list failed");
    mocks.getResearchThread.mockResolvedValue(thread);
    mocks.getResearchThreads.mockRejectedValue(listError);

    await expect(loadResearchThreadPage(THREAD_ID, 20)).rejects.toBe(listError);
  });
});
