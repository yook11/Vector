import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/image", () => ({
  default: () => null,
}));

import { SlimMasthead } from "./SlimMasthead";

describe("SlimMasthead", () => {
  it("renders the wordmark and the provided slots", () => {
    render(
      <SlimMasthead
        navSlot={<nav aria-label="主要ページ" />}
        mobileNavSlot={<button type="button">menu</button>}
        themeSlot={<button type="button">theme</button>}
        userMenuSlot={<span>user@example.com</span>}
      />,
    );

    expect(screen.getByText("VECTOR")).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "主要ページ" }),
    ).toBeInTheDocument();
    expect(screen.getByText("user@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "theme" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "menu" })).toBeInTheDocument();
  });
});
