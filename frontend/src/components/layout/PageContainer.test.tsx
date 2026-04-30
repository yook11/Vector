import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PageContainer } from "./PageContainer";

describe("PageContainer", () => {
  it("renders children inside a <main> landmark", () => {
    render(
      <PageContainer>
        <p>content</p>
      </PageContainer>,
    );
    const main = screen.getByRole("main");
    expect(main).toBeInTheDocument();
    expect(main).toContainHTML("<p>content</p>");
  });

  it("defaults to max-w-5xl and gap-8", () => {
    render(
      <PageContainer>
        <span data-testid="child" />
      </PageContainer>,
    );
    const inner = screen.getByTestId("child").parentElement;
    expect(inner).toHaveClass("max-w-5xl");
    expect(inner).toHaveClass("gap-8");
  });

  it.each([
    ["3xl", "max-w-3xl"],
    ["4xl", "max-w-4xl"],
    ["5xl", "max-w-5xl"],
  ] as const)("applies maxWidth=%s as %s", (maxWidth, expected) => {
    render(
      <PageContainer maxWidth={maxWidth}>
        <span data-testid="child" />
      </PageContainer>,
    );
    expect(screen.getByTestId("child").parentElement).toHaveClass(expected);
  });

  it.each([
    [8, "gap-8"],
    [10, "gap-10"],
    [12, "gap-12"],
  ] as const)("applies gap=%s as %s", (gap, expected) => {
    render(
      <PageContainer gap={gap}>
        <span data-testid="child" />
      </PageContainer>,
    );
    expect(screen.getByTestId("child").parentElement).toHaveClass(expected);
  });

  it("merges additional className onto inner container", () => {
    render(
      <PageContainer className="text-center">
        <span data-testid="child" />
      </PageContainer>,
    );
    expect(screen.getByTestId("child").parentElement).toHaveClass(
      "text-center",
    );
  });
});
