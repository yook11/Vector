import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DashboardArticleListSkeleton } from "./DashboardArticleListSkeleton";

describe("DashboardArticleListSkeleton", () => {
  it("shows a live update message and hides the decorative grid from assistive tech", () => {
    const { container } = render(<DashboardArticleListSkeleton />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("記事を更新中…");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-atomic", "true");
    expect(status).not.toHaveClass("sr-only");

    const grid = container.querySelector("[aria-hidden='true']");
    expect(grid).toBeInTheDocument();
    expect(grid?.children).toHaveLength(6);

    for (const placeholder of container.querySelectorAll(".animate-pulse")) {
      expect(placeholder).toHaveClass("motion-reduce:animate-none");
    }
  });
});
