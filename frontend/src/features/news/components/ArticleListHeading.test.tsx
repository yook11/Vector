import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ArticleListHeading } from "./ArticleListHeading";

describe("ArticleListHeading", () => {
  it("カテゴリを選んでいなければ「すべて」と表示する", () => {
    render(<ArticleListHeading categories={[{ slug: "ai", name: "AI" }]} />);

    expect(screen.getByText("すべて")).toBeInTheDocument();
  });

  it("選んだカテゴリが一覧にあればその名前を表示する", () => {
    render(
      <ArticleListHeading
        categories={[
          { slug: "ai", name: "AI" },
          { slug: "semiconductor", name: "半導体" },
        ]}
        selectedCategorySlug="ai"
      />,
    );

    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.queryByText("すべて")).not.toBeInTheDocument();
  });

  it("選んだカテゴリが一覧になければ slug を出さずに「存在しないカテゴリ」と表示する", () => {
    const { container } = render(
      <ArticleListHeading
        categories={[{ slug: "ai", name: "AI" }]}
        selectedCategorySlug="robotics"
      />,
    );

    expect(screen.getByText("存在しないカテゴリ")).toBeInTheDocument();
    expect(screen.queryByText("すべて")).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("robotics");
  });
});
