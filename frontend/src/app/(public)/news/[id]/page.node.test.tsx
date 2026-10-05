import {
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  article: vi.fn(),
  similar: vi.fn(),
  ids: vi.fn(),
  detailProps: vi.fn(),
  relatedProps: vi.fn(),
}));
vi.mock("@/features/news", () => ({
  getArticleById: mocks.article,
  getSimilarArticles: mocks.similar,
  NewsDetail: (props: unknown) => {
    mocks.detailProps(props);
    return null;
  },
  RelatedArticles: (props: unknown) => {
    mocks.relatedProps(props);
    return null;
  },
}));
vi.mock("@/features/watchlist/server", () => ({ getWatchlistIds: mocks.ids }));
vi.mock("@/components/layout/PageNavigation", () => ({
  PageNavigationContent: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/components/layout/ShellMasthead", () => ({
  ShellMasthead: () => null,
}));
vi.mock("@/components/paper", () => ({
  PaperSurface: ({ children }: { children: ReactNode }) => children,
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

// Server Component の木を、async な子も含めて最後まで評価する。
async function resolveServerTree(node: ReactNode): Promise<ReactNode> {
  if (Array.isArray(node)) {
    return Promise.all(node.map((child) => resolveServerTree(child)));
  }
  if (!isValidElement(node)) return node;
  if (typeof node.type === "function") {
    const component = node.type as (props: unknown) => ReactNode;
    return resolveServerTree(await component(node.props));
  }
  const element = node as ReactElement<{ children?: ReactNode }>;
  const children = await resolveServerTree(element.props.children);
  return cloneElement(element, undefined, children);
}

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
    // 本文の記事のウォッチ状態は URL の ID で、他の取得と同時に問い合わせる。
    expect(mocks.ids).toHaveBeenCalledWith([1]);
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

describe("記事詳細のウォッチ状態", () => {
  it("関連記事のウォッチ状態は、関連記事を取ってからその ID で問い合わせる", async () => {
    const related = [{ id: 2 }, { id: 3 }];
    mocks.similar.mockResolvedValue(related);
    mocks.ids.mockImplementation(async (articleIds: number[]) =>
      articleIds[0] === 1 ? new Set([1]) : new Set([3]),
    );

    await resolveServerTree(
      await NewsPage({ params: Promise.resolve({ id: "1" }) }),
    );

    expect(mocks.ids.mock.calls).toEqual([[[1]], [[2, 3]]]);
    expect(mocks.detailProps).toHaveBeenCalledWith(
      expect.objectContaining({ isWatched: true }),
    );
    expect(mocks.relatedProps).toHaveBeenCalledWith({
      articles: related,
      watchedIds: new Set([3]),
    });
  });

  it("関連記事のウォッチ状態が取れなければ、関連記事を出さずに本文は表示する", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    mocks.similar.mockResolvedValue([{ id: 2 }]);
    mocks.ids.mockImplementation(async (articleIds: number[]) => {
      if (articleIds[0] === 1) return new Set();
      throw new Error("backend unavailable");
    });

    await resolveServerTree(
      await NewsPage({ params: Promise.resolve({ id: "1" }) }),
    );

    expect(mocks.detailProps).toHaveBeenCalledOnce();
    expect(mocks.relatedProps).toHaveBeenCalledWith({
      articles: [],
      watchedIds: new Set(),
    });
  });
});
