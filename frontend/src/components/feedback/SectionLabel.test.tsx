import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SectionLabel } from "./SectionLabel";

describe("SectionLabel", () => {
  it("renders a <span> by default", () => {
    render(<SectionLabel>Hot Entities</SectionLabel>);
    const el = screen.getByText("Hot Entities");
    expect(el.tagName).toBe("SPAN");
  });

  it.each(["h2", "h3"] as const)("renders as <%s> when as=%s", (tag) => {
    render(<SectionLabel as={tag}>Section</SectionLabel>);
    expect(screen.getByText("Section").tagName).toBe(tag.toUpperCase());
  });

  it("applies the base typography class", () => {
    render(<SectionLabel>x</SectionLabel>);
    const el = screen.getByText("x");
    expect(el).toHaveClass("text-xs");
    expect(el).toHaveClass("uppercase");
    expect(el).toHaveClass("tracking-widest");
    expect(el).toHaveClass("text-muted-foreground");
  });

  it("merges additional className", () => {
    render(<SectionLabel className="font-semibold">x</SectionLabel>);
    expect(screen.getByText("x")).toHaveClass("font-semibold");
  });
});
