import { fireEvent, render, screen } from "@testing-library/react";
import type {
  AnchorHTMLAttributes,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const useSessionMock = vi.fn();
const usePathnameMock = vi.fn();
const useLinkStatusMock = vi.fn();
const linkNavigationMock = vi.fn();

// Link は href / aria-current / className を ShellNav から受けるため全 props を <a> へ forward する。
// useLinkStatus は NavPendingDot が読むため可変モックにする。
vi.mock("next/link", () => ({
  default: ({
    children,
    href,
    onClick,
    onNavigate,
    ...rest
  }: {
    children: ReactNode;
    href: string;
    onNavigate?: (event: { preventDefault: () => void }) => void;
  } & AnchorHTMLAttributes<HTMLAnchorElement>) => {
    function handleClick(event: ReactMouseEvent<HTMLAnchorElement>) {
      onClick?.(event);
      if (!event.defaultPrevented && onNavigate !== undefined) {
        onNavigate({ preventDefault: () => event.preventDefault() });
      }
      if (!event.defaultPrevented) linkNavigationMock(href);
      // JSDOM は実document navigationを実装しないため、このmock境界で止める。
      event.preventDefault();
    }

    return (
      <a href={href} onClick={handleClick} {...rest}>
        {children}
      </a>
    );
  },
  useLinkStatus: () => useLinkStatusMock(),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => usePathnameMock(),
}));
vi.mock("@/lib/auth/auth-client", () => ({
  useSession: () => useSessionMock(),
}));

import { ShellNav } from "./ShellNav";

describe("ShellNav", () => {
  beforeEach(() => {
    useSessionMock.mockReset();
    usePathnameMock.mockReset();
    useLinkStatusMock.mockReset();
    linkNavigationMock.mockReset();
    useLinkStatusMock.mockReturnValue({ pending: false });
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

  it("Research sectionではcurrent itemをno-opにしてglobal navigationを始めない", () => {
    useSessionMock.mockReturnValue({ data: null });
    usePathnameMock.mockReturnValue("/research/thread-1");
    render(<ShellNav />);

    const research = screen.getByRole("link", { name: "Research" });
    expect(research).toHaveAttribute("href", "/research");
    fireEvent.click(research);
    expect(linkNavigationMock).not.toHaveBeenCalled();
    expect(screen.queryByText("Researchを読み込み中…")).toBeNull();
  });

  describe("NavPendingDot の展開", () => {
    beforeEach(() => {
      useSessionMock.mockReturnValue({ data: null });
      usePathnameMock.mockReturnValue("/");
    });

    it("各 nav リンクに NavPendingDot (aria-hidden span) を1つずつ描画する", () => {
      render(<ShellNav />);

      const links = screen.getAllByRole("link");
      const dots = document.querySelectorAll("span[aria-hidden='true']");
      expect(links.length).toBeGreaterThan(0);
      expect(dots.length).toBe(links.length);
    });

    it("pending=false のとき全ての dot が opacity-0 で data-pending を持たない", () => {
      useLinkStatusMock.mockReturnValue({ pending: false });
      render(<ShellNav />);

      const dots = document.querySelectorAll("span[aria-hidden='true']");
      expect(dots.length).toBeGreaterThan(0);
      for (const dot of dots) {
        expect(dot).toHaveClass("opacity-0");
        expect(dot).not.toHaveAttribute("data-pending");
      }
    });

    it("pending=true のとき全ての dot が opacity-100 / data-pending を持つ", () => {
      useLinkStatusMock.mockReturnValue({ pending: true });
      render(<ShellNav />);

      const dots = document.querySelectorAll("span[aria-hidden='true']");
      expect(dots.length).toBeGreaterThan(0);
      for (const dot of dots) {
        expect(dot).toHaveClass("opacity-100");
        expect(dot).toHaveAttribute("data-pending", "");
      }
    });
  });
});
