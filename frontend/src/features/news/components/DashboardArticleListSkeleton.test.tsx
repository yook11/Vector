import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DashboardArticleListSkeleton } from "./DashboardArticleListSkeleton";

describe("DashboardArticleListSkeleton", () => {
  it("announces loading to assistive tech while hiding the visual placeholder", () => {
    const { container } = render(<DashboardArticleListSkeleton />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("記事を読み込み中");

    const grid = container.querySelector("[aria-hidden='true']");
    expect(grid).toBeInTheDocument();
    expect(grid?.children).toHaveLength(6);
  });
});
