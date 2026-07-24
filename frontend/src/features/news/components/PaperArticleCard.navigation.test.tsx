import { render, screen, waitFor } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PageNavigationProvider } from "@/components/layout/PageNavigation";
import type { ArticleBrief } from "@/types/types.gen";
import { PaperArticleCard } from "./PaperArticleCard";

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
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

const article = {
  id: 101,
  translatedTitle: "カードから開く記事",
  keyPoints: [],
  summaryPreview: "記事の概要",
  category: { name: "AI", slug: "ai" },
  source: { attributionLabel: "Vector", name: "Vector" },
  publishedAt: "2026-07-24T00:00:00Z",
} as unknown as ArticleBrief;

describe("PaperArticleCard navigation lifecycle", () => {
  beforeEach(() => {
    mocks.pendingByHref.clear();
  });

  it("記事detailへの遷移中はglobal pendingを開始し、settleで解除する", async () => {
    const tree = () => (
      <PageNavigationProvider>
        <PaperArticleCard article={article} />
      </PageNavigationProvider>
    );
    const view = render(tree());

    expect(
      screen.getByRole("link", { name: "カードから開く記事" }),
    ).toHaveAttribute("href", "/news/101");

    mocks.pendingByHref.set("/news/101", true);
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "記事を読み込み中…" }),
      ).toBeVisible(),
    );

    mocks.pendingByHref.set("/news/101", false);
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.queryByRole("status", { name: "記事を読み込み中…" }),
      ).toBeNull(),
    );
  });
});
