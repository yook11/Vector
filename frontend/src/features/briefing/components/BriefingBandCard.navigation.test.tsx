import { render, screen, waitFor } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PageNavigationProvider } from "@/components/layout/PageNavigation";
import type { ReadyBriefingCard } from "../page-models/briefing-list";
import { BriefingBandCard } from "./BriefingBandCard";

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
  usePathname: () => "/briefing",
  useSearchParams: () => new URLSearchParams(),
}));

const card: ReadyBriefingCard = {
  category: { slug: "ai", name: "AI" },
  weekStart: "2026-07-21",
  headline: "Briefing カード",
  summary: "カテゴリ別の要約",
  inputArticleCount: 3,
};

describe("BriefingBandCard navigation lifecycle", () => {
  beforeEach(() => {
    mocks.pendingByHref.clear();
  });

  it("detailへの遷移中はglobal pendingを開始し、settleで解除する", async () => {
    const tree = () => (
      <PageNavigationProvider>
        <BriefingBandCard card={card} currentWeekStart={card.weekStart} />
      </PageNavigationProvider>
    );
    const view = render(tree());

    expect(
      screen.getByRole("link", { name: /Briefing カード/ }),
    ).toHaveAttribute("href", "/briefing/ai");

    mocks.pendingByHref.set("/briefing/ai", true);
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "Briefingを読み込み中…" }),
      ).toBeVisible(),
    );

    mocks.pendingByHref.set("/briefing/ai", false);
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.queryByRole("status", { name: "Briefingを読み込み中…" }),
      ).toBeNull(),
    );
  });
});
