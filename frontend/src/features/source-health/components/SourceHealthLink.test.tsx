import { render, screen, waitFor } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  pendingByHref: new Map<string, boolean>(),
}));

type LinkMockProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  children: ReactNode;
  href: string;
  prefetch?: boolean | null;
  replace?: boolean;
  scroll?: boolean;
};

vi.mock("next/link", async () => {
  const React = await import("react");
  const PendingContext = React.createContext(false);

  function Link({
    children,
    href,
    prefetch: _prefetch,
    replace: _replace,
    scroll: _scroll,
    ...props
  }: LinkMockProps) {
    return (
      <PendingContext.Provider value={mocks.pendingByHref.get(href) ?? false}>
        <a href={href} {...props}>
          {children}
        </a>
      </PendingContext.Provider>
    );
  }

  return {
    default: Link,
    useLinkStatus: () => ({ pending: React.useContext(PendingContext) }),
  };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams(),
}));

import { PageNavigationProvider } from "@/components/layout/PageNavigation";
import { SourceHealthLink } from "./SourceHealthLink";

describe("SourceHealthLink", () => {
  beforeEach(() => {
    mocks.pendingByHref.clear();
  });

  it("/admin/source-health への link を表示する", () => {
    render(<SourceHealthLink />);
    const link = screen.getByRole("link", { name: /source health/i });
    expect(link).toHaveAttribute("href", "/admin/source-health");
  });

  it("遷移中はglobal pendingを開始し、settleで解除する", async () => {
    const tree = () => (
      <PageNavigationProvider>
        <SourceHealthLink />
      </PageNavigationProvider>
    );
    const view = render(tree());

    mocks.pendingByHref.set("/admin/source-health", true);
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "Source Healthを読み込み中…" }),
      ).toBeVisible(),
    );

    mocks.pendingByHref.set("/admin/source-health", false);
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.queryByRole("status", {
          name: "Source Healthを読み込み中…",
        }),
      ).toBeNull(),
    );
  });
});
