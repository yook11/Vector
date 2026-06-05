import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ProtectedNavItem } from "@/components/layout/nav-items";

vi.mock("next/image", () => ({
  default: () => null,
}));
vi.mock("@/components/layout/MobileNav", () => ({
  MobileNav: () => null,
}));

import { SlimMasthead } from "./SlimMasthead";

const navItems: ProtectedNavItem[] = [
  { href: "/", label: "ニュース", icon: "news" },
  { href: "/briefing", label: "Briefing", icon: "briefing" },
  { href: "/watchlist", label: "ウォッチリスト", icon: "watchlist" },
];

describe("SlimMasthead", () => {
  it("renders wordmark, nav items, slots, and marks the active page", () => {
    render(
      <SlimMasthead
        navItems={navItems}
        activeHref="/"
        themeSlot={<button type="button">theme</button>}
        userMenuSlot={<span>user@example.com</span>}
      />,
    );

    expect(screen.getByText("VECTOR")).toBeInTheDocument();

    const newsLink = screen.getByRole("link", { name: "ニュース" });
    expect(newsLink).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Briefing" })).not.toHaveAttribute(
      "aria-current",
    );

    expect(screen.getByText("user@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "theme" })).toBeInTheDocument();
  });
});
