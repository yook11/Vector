import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireSession: vi.fn(),
  getWatchlist: vi.fn(),
}));
vi.mock("@/lib/auth/guards", () => ({
  requireSessionForAction: mocks.requireSession,
}));
vi.mock("./get-watchlist", () => ({ getWatchlist: mocks.getWatchlist }));

import { loadMoreWatchlist } from "./load-more-watchlist";

beforeEach(() => {
  mocks.requireSession.mockReset().mockResolvedValue({ user: { id: "u1" } });
  mocks.getWatchlist
    .mockReset()
    .mockResolvedValue({ items: [], nextCursor: null });
});

describe("ウォッチリストの続きの読み込み", () => {
  it("ログインを確かめてから、カーソルを渡して続きを取る", async () => {
    await loadMoreWatchlist("eyJpZCI6MX0");

    expect(mocks.requireSession).toHaveBeenCalledOnce();
    expect(mocks.getWatchlist).toHaveBeenCalledWith("eyJpZCI6MX0");
  });

  it("未ログインなら backend を呼ばない", async () => {
    mocks.requireSession.mockRejectedValue(new Error("NEXT_REDIRECT"));

    await expect(loadMoreWatchlist("eyJpZCI6MX0")).rejects.toThrow();
    expect(mocks.getWatchlist).not.toHaveBeenCalled();
  });

  it("形の不正なカーソルは backend を呼ばずに拒否する", async () => {
    await expect(loadMoreWatchlist("not base64!")).rejects.toThrow();
    expect(mocks.getWatchlist).not.toHaveBeenCalled();
  });
});
