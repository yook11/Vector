import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BriefingArticleEmbedParsed } from "../schemas/briefing";
import { ArticleCard } from "./ArticleCard";

function makeArticle(
  overrides: Partial<BriefingArticleEmbedParsed> = {},
): BriefingArticleEmbedParsed {
  return {
    id: 42,
    translatedTitle: "AI が医療診断を革新する",
    source: { name: "TechCrunch", attributionLabel: "TechCrunch (US)" },
    url: "https://techcrunch.com/example",
    publishedAt: "2026-06-01T00:00:00+00:00",
    keyPoints: ["診断精度が人間医師を上回る", "FDA が仮承認"],
    ...overrides,
  };
}

describe("ArticleCard — タイトルリンク", () => {
  it("タイトルが /news/{id} への内部リンクである", () => {
    render(<ArticleCard article={makeArticle({ id: 99 })} />);
    // 完全一致で取得 (外部原文ボタンの aria-label がタイトルを含むため regex では複数マッチする)
    const link = screen.getByRole("link", {
      name: "AI が医療診断を革新する",
    });
    expect(link).toHaveAttribute("href", "/news/99");
  });

  it("タイトルテキストが表示される", () => {
    render(<ArticleCard article={makeArticle()} />);
    expect(screen.getByText("AI が医療診断を革新する")).toBeInTheDocument();
  });
});

describe("ArticleCard — 外部原文ボタン", () => {
  it("article.url を href に持つ外部リンクがある", () => {
    render(
      <ArticleCard
        article={makeArticle({ url: "https://example.com/article" })}
      />,
    );
    const link = screen.getByRole("link", { name: /原文を読む/ });
    expect(link).toHaveAttribute("href", "https://example.com/article");
  });

  it("外部リンクは target=_blank である", () => {
    render(<ArticleCard article={makeArticle()} />);
    const link = screen.getByRole("link", { name: /原文を読む/ });
    expect(link).toHaveAttribute("target", "_blank");
  });
});

describe("ArticleCard — 出典表示", () => {
  it("attributionLabel が非 null のときその文字列が表示される", () => {
    render(
      <ArticleCard
        article={makeArticle({
          source: { name: "TechCrunch", attributionLabel: "TechCrunch (US)" },
        })}
      />,
    );
    expect(screen.getByText("TechCrunch (US)")).toBeInTheDocument();
  });

  it("attributionLabel が null のとき source.name に fallback して表示される", () => {
    render(
      <ArticleCard
        article={makeArticle({
          source: { name: "Wired", attributionLabel: null },
        })}
      />,
    );
    // PaperByline が sourceLabel を表示する。fallback は source.name
    expect(screen.getByText("Wired")).toBeInTheDocument();
  });
});

describe("ArticleCard — keyPoints", () => {
  it("keyPoints が全件描画される", () => {
    const points = ["要点A", "要点B", "要点C"];
    render(<ArticleCard article={makeArticle({ keyPoints: points })} />);
    for (const point of points) {
      expect(screen.getByText(point)).toBeInTheDocument();
    }
  });

  it("keyPoints が空配列のときリスト要素が描画されない", () => {
    render(<ArticleCard article={makeArticle({ keyPoints: [] })} />);
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });
});
