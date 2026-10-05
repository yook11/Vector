import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getArticles: vi.fn() }));
vi.mock("./get-articles", () => ({ getArticles: mocks.getArticles }));
vi.mock("@/lib/cache/article-list-revision", () => ({
  getArticleListRevision: () => "revision-a",
}));

import { loadMoreArticles } from "./load-more-articles";

beforeEach(() => {
  mocks.getArticles
    .mockReset()
    .mockResolvedValue({ items: [], nextCursor: null });
});

describe("記事一覧の続きの読み込み", () => {
  it("カテゴリとカーソルをこの順で渡して続きを取る", async () => {
    await loadMoreArticles("ai", "eyJpZCI6MX0");

    expect(mocks.getArticles).toHaveBeenCalledWith(
      { category: "ai", cursor: "eyJpZCI6MX0" },
      "revision-a",
    );
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
