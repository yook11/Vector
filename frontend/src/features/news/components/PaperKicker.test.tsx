import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PaperKicker } from "./PaperKicker";

describe("PaperKicker", () => {
  it("renders the category code derived from the slug and the display name", () => {
    render(<PaperKicker slug="security" name="セキュリティ" />);

    expect(screen.getByText("SECURITY")).toBeInTheDocument();
    expect(screen.getByText("セキュリティ")).toBeInTheDocument();
  });
});
