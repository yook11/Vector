import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PipelineHealthResponse } from "@/types/types.gen";

const mocks = vi.hoisted(() => ({
  getPipelineStatus: vi.fn(),
}));

vi.mock("../api/get-pipeline-status", () => ({
  getPipelineStatus: mocks.getPipelineStatus,
}));

import { getPipelineStatusViewModel } from "./pipeline-status";

beforeEach(() => {
  vi.clearAllMocks();
});

const sample: PipelineHealthResponse = {
  summary: {
    failedEventCount24h: 3,
    backfillTargetTotal: 12,
    oldestBackfillTargetAgeSeconds: 4320,
    completionQueueCount: 5,
    oldestCompletionQueueAgeSeconds: 60,
    observedAt: "2026-06-03T00:00:00Z",
    eventWindowStart: "2026-06-02T00:00:00Z",
  },
  stages: [],
};

describe("getPipelineStatusViewModel", () => {
  it("getPipelineStatus の結果を透過して返す", async () => {
    mocks.getPipelineStatus.mockResolvedValue(sample);
    const result = await getPipelineStatusViewModel();
    expect(result).toEqual(sample);
  });

  it("getPipelineStatus を 1 度だけ呼ぶ", async () => {
    mocks.getPipelineStatus.mockResolvedValue(sample);
    await getPipelineStatusViewModel();
    expect(mocks.getPipelineStatus).toHaveBeenCalledTimes(1);
  });
});
