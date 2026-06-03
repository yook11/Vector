import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ArticleBrief } from "@/types/types.gen";
import { PaperArticleCard } from "./PaperArticleCard";

const article: ArticleBrief = {
  id: 101,
  translatedTitle: "Claude Mythosが明らかにした遅延問題",
  summary:
    "AIによる脆弱性の悪用までの時間が劇的に短縮されつつあり、企業のパッチ体制が追いついていない。",
  category: {
    name: "セキュリティ",
    slug: "security",
  },
  source: {
    attributionLabel: "Hacker News",
    name: "Hacker News",
  },
  publishedAt: "2026-05-31T02:30:00.000Z",
};

describe("PaperArticleCard", () => {
  it("renders article title, summary, category, source, date, and action", () => {
    render(
      <PaperArticleCard
        article={article}
        actionSlot={<button type="button">保存</button>}
      />,
    );

    expect(
      screen.getByRole("link", { name: article.translatedTitle }),
    ).toHaveAttribute("href", "/news/101");
    expect(screen.getByText(article.summary)).toBeInTheDocument();
    expect(screen.getByText(article.category.name)).toBeInTheDocument();
    expect(screen.getByText("Hacker News")).toBeInTheDocument();
    expect(screen.getByText("2026年5月31日")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeInTheDocument();
  });
});
