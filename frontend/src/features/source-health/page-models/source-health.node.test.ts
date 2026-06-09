import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SourceHealthResponse } from "@/types/types.gen";

const mocks = vi.hoisted(() => ({
  getSourceHealth: vi.fn(),
}));

vi.mock("../api/get-source-health", () => ({
  getSourceHealth: mocks.getSourceHealth,
}));

import { getSourceHealthViewModel } from "./source-health";

beforeEach(() => {
  vi.clearAllMocks();
});

const sample: SourceHealthResponse = {
  windowHours: 48,
  observedAt: "2026-06-03T00:00:00Z",
  items: [],
};

describe("getSourceHealthViewModel", () => {
  it("window label を windowHours に変換して getSourceHealth を呼ぶ", async () => {
    mocks.getSourceHealth.mockResolvedValue(sample);
    await getSourceHealthViewModel("48h");
    expect(mocks.getSourceHealth).toHaveBeenCalledWith(48);
  });

  it("getSourceHealth の結果を透過して返す", async () => {
    mocks.getSourceHealth.mockResolvedValue(sample);
    const result = await getSourceHealthViewModel("24h");
    expect(result).toEqual(sample);
  });

  it("getSourceHealth を 1 度だけ呼ぶ", async () => {
    mocks.getSourceHealth.mockResolvedValue(sample);
    await getSourceHealthViewModel("7d");
    expect(mocks.getSourceHealth).toHaveBeenCalledTimes(1);
  });
});
