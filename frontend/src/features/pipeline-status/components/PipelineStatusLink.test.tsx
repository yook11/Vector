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
import { PipelineStatusLink } from "./PipelineStatusLink";

describe("PipelineStatusLink", () => {
  beforeEach(() => {
    mocks.pendingByHref.clear();
  });

  it("/admin/pipeline-status への link を表示する", () => {
    render(<PipelineStatusLink />);
    const link = screen.getByRole("link", { name: /pipeline status/i });
    expect(link).toHaveAttribute("href", "/admin/pipeline-status");
  });

  it("遷移中はglobal pendingを開始し、settleで解除する", async () => {
    const tree = () => (
      <PageNavigationProvider>
        <PipelineStatusLink />
      </PageNavigationProvider>
    );
    const view = render(tree());

    mocks.pendingByHref.set("/admin/pipeline-status", true);
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "Pipeline Statusを読み込み中…" }),
      ).toBeVisible(),
    );

    mocks.pendingByHref.set("/admin/pipeline-status", false);
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.queryByRole("status", {
          name: "Pipeline Statusを読み込み中…",
        }),
      ).toBeNull(),
    );
  });
});
