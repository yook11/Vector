import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type {
  AnalyzedArticlePreview,
  AnalyzedArticlePreviewList,
} from "@/types/types.gen";

vi.mock("next/navigation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("next/navigation")>()),
  useRouter: () => ({ bfcacheId: "navigation-1" }),
}));

// 押すと操作が成功したものとして、切り替えた状態を知らせる。
vi.mock("@/features/watchlist", () => ({
  WatchlistButton: ({
    articleId,
    isWatched,
    onWatchedChange,
  }: {
    articleId: number;
    isWatched: boolean;
    onWatchedChange?: (isWatched: boolean) => void;
  }) => (
    // biome-ignore lint/a11y/useKeyWithClickEvents: 試験用の代役
    // biome-ignore lint/a11y/noStaticElementInteractions: 試験用の代役
    <span
      data-testid={`watch-${articleId}`}
      onClick={() => onWatchedChange?.(!isWatched)}
    >
      {isWatched ? "ウォッチ中" : "未ウォッチ"}
    </span>
  ),
}));

import { LoadMoreArticleList } from "./LoadMoreArticleList";

function article(id: number, translatedTitle: string): AnalyzedArticlePreview {
  return {
    id,
    translatedTitle,
    keyPoints: [],
    summaryPreview: "要約",
    category: { slug: "ai", name: "AI" },
    source: { name: "Hacker News", attributionLabel: null },
    publishedAt: "2026-10-05T00:00:00Z",
  };
}

function titles(): string[] {
  return screen
    .getAllByRole("heading", { level: 2 })
    .map((heading) => heading.textContent ?? "");
}

const emptyState = <p>記事がありません</p>;

describe("LoadMoreArticleList", () => {
  it("続きがあれば、最初の記事と「さらに読み込む」を出す", () => {
    render(
      <LoadMoreArticleList
        initialList={{ items: [article(2, "記事2")], nextCursor: "cursor-1" }}
        initialWatchedIds={new Set()}
        loadMore={vi.fn()}
        emptyState={emptyState}
      />,
    );

    expect(titles()).toEqual(["記事2"]);
    expect(
      screen.getByRole("button", { name: "さらに読み込む" }),
    ).toBeInTheDocument();
  });

  it("「さらに読み込む」を押すと、カーソルを渡して続きを下に足す", async () => {
    const loadMore = vi.fn().mockResolvedValue({
      list: {
        items: [article(1, "記事1")],
        nextCursor: "cursor-2",
      } satisfies AnalyzedArticlePreviewList,
      watchedIds: new Set(),
    });
    render(
      <LoadMoreArticleList
        initialList={{ items: [article(2, "記事2")], nextCursor: "cursor-1" }}
        initialWatchedIds={new Set()}
        loadMore={loadMore}
        emptyState={emptyState}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "さらに読み込む" }),
    );

    expect(loadMore).toHaveBeenCalledWith("cursor-1");
    expect(titles()).toEqual(["記事2", "記事1"]);
    expect(
      screen.getByRole("button", { name: "さらに読み込む" }),
    ).toBeInTheDocument();
  });

  it("続きを最後まで読むと、ボタンを消して末尾に達したことを示す", async () => {
    const loadMore = vi.fn().mockResolvedValue({
      list: { items: [article(1, "記事1")], nextCursor: null },
      watchedIds: new Set(),
    });
    render(
      <LoadMoreArticleList
        initialList={{ items: [article(2, "記事2")], nextCursor: "cursor-1" }}
        initialWatchedIds={new Set()}
        loadMore={loadMore}
        emptyState={emptyState}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "さらに読み込む" }),
    );

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("これ以上の記事はありません")).toBeInTheDocument();
  });

  it("最初から続きがなければ、ボタンも末尾の表示も出さない", () => {
    render(
      <LoadMoreArticleList
        initialList={{ items: [article(2, "記事2")], nextCursor: null }}
        initialWatchedIds={new Set()}
        loadMore={vi.fn()}
        emptyState={emptyState}
      />,
    );

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(
      screen.queryByText("これ以上の記事はありません"),
    ).not.toBeInTheDocument();
  });

  it("読み込みに失敗すると再試行を出し、押すと同じカーソルで取り直す", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const loadMore = vi
      .fn()
      .mockRejectedValueOnce(new Error("backend unavailable"))
      .mockResolvedValueOnce({
        list: { items: [article(1, "記事1")], nextCursor: null },
        watchedIds: new Set(),
      });
    render(
      <LoadMoreArticleList
        initialList={{ items: [article(2, "記事2")], nextCursor: "cursor-1" }}
        initialWatchedIds={new Set()}
        loadMore={loadMore}
        emptyState={emptyState}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "さらに読み込む" }),
    );
    expect(screen.getByText("読み込めませんでした")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "もう一度読み込む" }),
    );

    expect(loadMore.mock.calls).toEqual([["cursor-1"], ["cursor-1"]]);
    expect(titles()).toEqual(["記事2", "記事1"]);
    expect(screen.queryByText("読み込めませんでした")).not.toBeInTheDocument();
  });

  it("最初の記事は渡されたウォッチ ID で、足した記事は読み込みで得たウォッチ状態で表示する", async () => {
    const loadMore = vi.fn().mockResolvedValue({
      list: { items: [article(1, "記事1")], nextCursor: null },
      watchedIds: new Set([1]),
    });
    render(
      <LoadMoreArticleList
        initialList={{
          items: [article(3, "記事3"), article(2, "記事2")],
          nextCursor: "cursor-1",
        }}
        initialWatchedIds={new Set([3])}
        loadMore={loadMore}
        emptyState={emptyState}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "さらに読み込む" }),
    );

    expect(screen.getByTestId("watch-3")).toHaveTextContent("ウォッチ中");
    expect(screen.getByTestId("watch-2")).toHaveTextContent("未ウォッチ");
    expect(screen.getByTestId("watch-1")).toHaveTextContent("ウォッチ中");
  });

  it("足した記事のウォッチを切り替えると、その記事の表示が切り替わる", async () => {
    const loadMore = vi.fn().mockResolvedValue({
      list: { items: [article(1, "記事1")], nextCursor: null },
      watchedIds: new Set([1]),
    });
    render(
      <LoadMoreArticleList
        initialList={{ items: [article(2, "記事2")], nextCursor: "cursor-1" }}
        initialWatchedIds={new Set()}
        loadMore={loadMore}
        emptyState={emptyState}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "さらに読み込む" }),
    );

    await userEvent.click(screen.getByTestId("watch-1"));

    expect(titles()).toEqual(["記事2", "記事1"]);
    expect(screen.getByTestId("watch-1")).toHaveTextContent("未ウォッチ");
  });

  it("showsOnlyWatched では、ウォッチを外した記事を読み込み済みの一覧から外す", async () => {
    render(
      <LoadMoreArticleList
        initialList={{
          items: [article(2, "記事2"), article(1, "記事1")],
          nextCursor: null,
        }}
        initialWatchedIds={new Set([1, 2])}
        loadMore={vi.fn()}
        emptyState={emptyState}
        showsOnlyWatched
      />,
    );

    await userEvent.click(screen.getByTestId("watch-2"));

    expect(titles()).toEqual(["記事1"]);
  });

  it("出す記事が無く続きもなければ、空の表示を出す", () => {
    render(
      <LoadMoreArticleList
        initialList={{ items: [], nextCursor: null }}
        initialWatchedIds={new Set()}
        loadMore={vi.fn()}
        emptyState={emptyState}
      />,
    );

    expect(screen.getByText("記事がありません")).toBeInTheDocument();
  });

  it("nextCursor の無い応答 (反映中の旧 backend) は続きなしとして扱う", () => {
    const legacyResponse = {
      items: [article(2, "記事2")],
    } as unknown as AnalyzedArticlePreviewList;
    render(
      <LoadMoreArticleList
        initialList={legacyResponse}
        initialWatchedIds={new Set()}
        loadMore={vi.fn()}
        emptyState={emptyState}
      />,
    );

    expect(titles()).toEqual(["記事2"]);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
