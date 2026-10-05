import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getArticles: vi.fn(),
  getWatchlistIds: vi.fn(),
}));
vi.mock("./get-articles", () => ({ getArticles: mocks.getArticles }));
vi.mock("@/features/watchlist/server", () => ({
  getWatchlistIds: mocks.getWatchlistIds,
}));
vi.mock("@/lib/cache/article-list-revision", () => ({
  getArticleListRevision: () => "revision-a",
}));

import { loadMoreArticles } from "./load-more-articles";

beforeEach(() => {
  mocks.getArticles
    .mockReset()
    .mockResolvedValue({ items: [], nextCursor: null });
  mocks.getWatchlistIds.mockReset().mockResolvedValue(new Set());
});

describe("記事一覧の続きの読み込み", () => {
  it("カテゴリとカーソルをこの順で渡して続きを取る", async () => {
    await loadMoreArticles("ai", "eyJpZCI6MX0");

    expect(mocks.getArticles).toHaveBeenCalledWith(
      { category: "ai", cursor: "eyJpZCI6MX0" },
      "revision-a",
    );
  });

  it("続きの記事の ID でウォッチ状態を問い合わせ、記事と一緒に返す", async () => {
    const list = {
      items: [{ id: 12 }, { id: 11 }],
      nextCursor: "eyJpZCI6MTF9",
    };
    mocks.getArticles.mockResolvedValue(list);
    mocks.getWatchlistIds.mockResolvedValue(new Set([11]));

    const loaded = await loadMoreArticles("ai", "eyJpZCI6MX0");

    expect(mocks.getWatchlistIds).toHaveBeenCalledWith([12, 11]);
    expect(loaded).toEqual({ list, watchedIds: new Set([11]) });
  });

  it("カテゴリが無ければカーソルだけを渡す", async () => {
    await loadMoreArticles(null, "eyJpZCI6MX0");

    expect(mocks.getArticles).toHaveBeenCalledWith(
      { cursor: "eyJpZCI6MX0" },
      "revision-a",
    );
  });

  it.each([
    ["カテゴリの形が不正", "../admin", "eyJpZCI6MX0"],
    ["カーソルに base64url 以外の文字", "ai", "not base64!"],
    ["カーソルが空", "ai", ""],
    ["カーソルが 257 字", "ai", "A".repeat(257)],
  ])("%s なら backend を呼ばずに拒否する", async (_, category, cursor) => {
    await expect(loadMoreArticles(category, cursor)).rejects.toThrow();
    expect(mocks.getArticles).not.toHaveBeenCalled();
  });
});
