import { render, screen } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const linkPropsMock = vi.hoisted(() => vi.fn());
const pathnameMock = vi.hoisted(() => vi.fn());

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  children: ReactNode;
  href: string;
  prefetch?: boolean | null;
  replace?: boolean;
  scroll?: boolean;
};

vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: LinkProps) => {
    linkPropsMock({ children, href, ...rest });
    return (
      <a href={href} {...rest}>
        {children}
      </a>
    );
  },
}));

vi.mock("next/navigation", () => ({
  usePathname: () => pathnameMock(),
}));

import { NavLink } from "./NavLink";

describe("NavLink", () => {
  beforeEach(() => {
    pathnameMock.mockReset();
    pathnameMock.mockReturnValue("/");
    linkPropsMock.mockReset();
  });

  it("internal Linkのhref、replace、scroll、prefetchをそのままNext Linkへ渡す", () => {
    render(
      <NavLink href="/briefing?period=week" prefetch replace scroll={false}>
        Briefing
      </NavLink>,
    );

    expect(screen.getByRole("link", { name: "Briefing" })).toHaveAttribute(
      "href",
      "/briefing?period=week",
    );
    expect(linkPropsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        href: "/briefing?period=week",
        prefetch: true,
        replace: true,
        scroll: false,
      }),
    );
  });

  it("external targetとdownload属性をnative anchorへ維持する", () => {
    render(
      <NavLink
        href="https://example.com/report.pdf"
        target="_blank"
        download="report.pdf"
      >
        外部レポート
      </NavLink>,
    );

    const link = screen.getByRole("link", { name: "外部レポート" });
    expect(link).toHaveAttribute("href", "https://example.com/report.pdf");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("download", "report.pdf");
  });
});
