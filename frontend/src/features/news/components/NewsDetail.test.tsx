import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ArticleDetail } from "@/types/types.gen";

vi.mock("@/features/watchlist", () => ({
  WatchlistButton: () => null,
}));

import { NewsDetail } from "./NewsDetail";

const article: ArticleDetail = {
  id: 42,
  translatedTitle: "テラデータ、AI投資のため従業員の昇給を見送り",
  summary: "第一段落のリード文。\n\n第二段落の本文。",
  investorTake: "背景整理の段落その一。\n\n背景整理の段落その二。",
  analyzedAt: "2026-06-05T04:00:00.000Z",
  category: { slug: "other", name: "市場・規制" },
  source: { name: "Hacker News", attributionLabel: "Hacker News" },
  publishedAt: "2026-06-05T02:54:00.000Z",
  original: {
    title: "CEO to staff: You're not getting a raise.",
    url: "https://example.com/orig",
  },
};

describe("NewsDetail", () => {
  it("renders kicker, title, deck, summary paragraphs, context note and links", () => {
    render(<NewsDetail article={article} isWatched={false} />);

    expect(
      screen.getByRole("heading", { level: 1, name: article.translatedTitle }),
    ).toBeInTheDocument();
    expect(screen.getByText(article.original.title)).toBeInTheDocument();

    // Kicker: slug "other" → code "MARKET" + 表示名
    expect(screen.getByText("MARKET")).toBeInTheDocument();
    expect(screen.getByText("市場・規制")).toBeInTheDocument();

    // summary は \n\n で段落分割される
    expect(screen.getByText("第一段落のリード文。")).toBeInTheDocument();
    expect(screen.getByText("第二段落の本文。")).toBeInTheDocument();

    // 背景ノートは中立語彙の見出しで investorTake を表示する
    expect(screen.getByText("CONTEXT")).toBeInTheDocument();
    expect(screen.getByText("文脈")).toBeInTheDocument();
    expect(screen.getByText("編集部による背景整理")).toBeInTheDocument();
    expect(screen.getByText("背景整理の段落その一。")).toBeInTheDocument();
    expect(screen.getByText("背景整理の段落その二。")).toBeInTheDocument();

    expect(screen.getByRole("link", { name: /原文を読む/ })).toHaveAttribute(
      "href",
      article.original.url,
    );
    expect(
      screen.getByRole("link", { name: /ダッシュボードに戻る/ }),
    ).toHaveAttribute("href", "/");
  });

  it("omits the context note when investorTake is empty", () => {
    render(<NewsDetail article={{ ...article, investorTake: "" }} isWatched />);

    expect(screen.queryByText("CONTEXT")).not.toBeInTheDocument();
    expect(screen.queryByText("文脈")).not.toBeInTheDocument();
  });
});
