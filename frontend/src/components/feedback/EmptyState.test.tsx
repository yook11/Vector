import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EmptyState } from "./EmptyState";

describe("EmptyState", () => {
  it("renders the title with role=status", () => {
    render(<EmptyState title="No articles found" />);
    const status = screen.getByRole("status");
    expect(status).toBeInTheDocument();
    expect(status).toHaveTextContent("No articles found");
  });

  it("omits description when not provided", () => {
    render(<EmptyState title="Empty" />);
    const status = screen.getByRole("status");
    expect(status.querySelectorAll("p")).toHaveLength(1);
  });

  it("renders description when provided", () => {
    render(
      <EmptyState
        title="No saved articles"
        description="Add some from the dashboard"
      />,
    );
    expect(screen.getByText("No saved articles")).toBeInTheDocument();
    expect(screen.getByText("Add some from the dashboard")).toBeInTheDocument();
  });

  it("merges additional className onto the status container", () => {
    render(<EmptyState title="x" className="bg-card" />);
    expect(screen.getByRole("status")).toHaveClass("bg-card");
  });
});
