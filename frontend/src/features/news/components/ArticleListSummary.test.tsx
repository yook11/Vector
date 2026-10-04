import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ArticleListSummary } from "./ArticleListSummary";

describe("ArticleListSummary", () => {
  it("カテゴリを選んでいなければ「すべて」と件数を表示する", async () => {
    render(
      await ArticleListSummary({
        articlesPromise: Promise.resolve({
          items: [],
          total: 42,
          page: 1,
          perPage: 20,
          totalPages: 3,
        }),
        categories: [{ slug: "ai", name: "AI" }],
      }),
    );

    expect(screen.getByText("すべて")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("選んだカテゴリが一覧にあればその名前を表示する", async () => {
    render(
      await ArticleListSummary({
        articlesPromise: Promise.resolve({
          items: [],
          total: 5,
          page: 1,
          perPage: 20,
          totalPages: 1,
        }),
        categories: [
          { slug: "ai", name: "AI" },
          { slug: "semiconductor", name: "半導体" },
        ],
        selectedCategorySlug: "ai",
      }),
    );

    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.queryByText("すべて")).not.toBeInTheDocument();
  });

  it("選んだカテゴリが一覧になければ slug を出さずに「存在しないカテゴリ」と表示する", async () => {
    const { container } = render(
      await ArticleListSummary({
        articlesPromise: Promise.resolve({
          items: [],
          total: 0,
          page: 1,
          perPage: 20,
          totalPages: 0,
        }),
        categories: [{ slug: "ai", name: "AI" }],
        selectedCategorySlug: "robotics",
      }),
    );

    expect(screen.getByText("存在しないカテゴリ")).toBeInTheDocument();
    expect(screen.queryByText("すべて")).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("robotics");
  });
});
