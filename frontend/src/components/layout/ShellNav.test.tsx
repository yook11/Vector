import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const useSessionMock = vi.fn();
const usePathnameMock = vi.fn();

vi.mock("@/lib/auth/auth-client", () => ({
  useSession: () => useSessionMock(),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => usePathnameMock(),
}));

import { ShellNav } from "./ShellNav";

describe("ShellNav", () => {
  beforeEach(() => {
    useSessionMock.mockReset();
    usePathnameMock.mockReset();
  });

  it("renders base nav and marks the active page by pathname", () => {
    useSessionMock.mockReturnValue({ data: null });
    usePathnameMock.mockReturnValue("/briefing");

    render(<ShellNav />);

    expect(screen.getByRole("link", { name: "Briefing" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "ニュース" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("hides the admin item until the session role is admin", () => {
    usePathnameMock.mockReturnValue("/");

    useSessionMock.mockReturnValue({ data: { user: { role: "user" } } });
    const { rerender } = render(<ShellNav />);
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();

    useSessionMock.mockReturnValue({ data: { user: { role: "admin" } } });
    rerender(<ShellNav />);
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
  });
});
