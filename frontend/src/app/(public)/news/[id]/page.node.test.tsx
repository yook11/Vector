import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  article: vi.fn(),
  similar: vi.fn(),
  ids: vi.fn(),
}));
vi.mock("@/features/news", () => ({
  getArticleById: mocks.article,
  getSimilarArticles: mocks.similar,
  NewsDetail: () => null,
  RelatedArticles: () => null,
}));
vi.mock("@/features/watchlist", () => ({ getWatchlistIds: mocks.ids }));
vi.mock("@/components/layout/PageNavigation", () => ({
  PageNavigationContent: () => null,
}));
vi.mock("@/components/layout/ShellMasthead", () => ({
  ShellMasthead: () => null,
}));
vi.mock("@/components/paper", () => ({
  PaperSurface: () => null,
  PaperTexture: () => null,
}));
vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NOT_FOUND");
  },
}));
vi.mock("@/lib/auth/guards", () => ({
  getCurrentSession: () => null,
  requireSession: () => {
    throw new Error("LOGIN_REQUIRED");
  },
}));

import NewsPage, { generateMetadata } from "./page";

beforeEach(() => {
  vi.clearAllMocks();
  mocks.article.mockResolvedValue({ id: 1, translatedTitle: "公開記事" });
  mocks.similar.mockResolvedValue([]);
  mocks.ids.mockResolvedValue(new Set());
});
describe("未登録での記事閲覧", () => {
  it("記事・関連記事の取得を開始できる", async () => {
    await expect(
      NewsPage({ params: Promise.resolve({ id: "1" }) }),
    ).resolves.toBeTruthy();
    expect(mocks.article).toHaveBeenCalledWith(1);
    expect(mocks.similar).toHaveBeenCalledWith(1, 5);
  });
  it("未ログインにも記事タイトルを公開する", async () => {
    expect(
      await generateMetadata({ params: Promise.resolve({ id: "1" }) }),
    ).toEqual({ title: "公開記事 | Vector" });
  });
  it.each(["abc", "0", "-1", "1.5"])("不正ID %s は取得前に404", async (id) => {
    await expect(NewsPage({ params: Promise.resolve({ id }) })).rejects.toThrow(
      "NOT_FOUND",
    );
    expect(mocks.article).not.toHaveBeenCalled();
    expect(mocks.similar).not.toHaveBeenCalled();
  });
  it("存在しない記事タイトルは404用表示", async () => {
    mocks.article.mockResolvedValue(null);
    expect(
      await generateMetadata({ params: Promise.resolve({ id: "999" }) }),
    ).toEqual({ title: "Article Not Found | Vector" });
  });
});
