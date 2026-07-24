import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PageNavigationProvider } from "@/components/layout/PageNavigation";
import type { ResearchInternalArticleSource } from "@/types/types.gen";
import { CitedAnswerContent } from "./CitedAnswerContent";

const mocks = vi.hoisted(() => ({
  pathname: "/research/thread-1",
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
    href: _href,
    prefetch: _prefetch,
    replace: _replace,
    scroll: _scroll,
    ...props
  }: LinkMockProps) {
    const [pending, setPending] = React.useState(false);

    return (
      <PendingContext.Provider value={pending}>
        <a
          {...props}
          href={_href}
          onClick={(event) => {
            props.onClick?.(event);
            if (!event.defaultPrevented) setPending(true);
            event.preventDefault();
          }}
        >
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
  usePathname: () => mocks.pathname,
  useSearchParams: () => new URLSearchParams(),
}));

const internalArticle: ResearchInternalArticleSource = {
  kind: "internal_article",
  sourceRef: "1",
  articleId: 42,
  title: "Research 内部記事",
  publishedAt: "2026-07-24T00:00:00Z",
};

describe("CitedAnswerContent internal article navigation lifecycle", () => {
  beforeEach(() => {
    mocks.pathname = "/research/thread-1";
  });

  it("内部記事citationのdetail遷移中はglobal pendingを開始し、settleで解除する", async () => {
    const user = userEvent.setup();
    const tree = () => (
      <PageNavigationProvider>
        <CitedAnswerContent
          content="内部記事への引用 [[1]]"
          sources={[internalArticle]}
        />
      </PageNavigationProvider>
    );
    const view = render(tree());

    await user.click(screen.getByRole("button", { name: "出典 1" }));
    expect(
      screen.getByRole("link", { name: "Research 内部記事" }),
    ).toHaveAttribute("href", "/news/42");

    await user.click(screen.getByRole("link", { name: "Research 内部記事" }));
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "記事を読み込み中…" }),
      ).toBeVisible(),
    );

    mocks.pathname = "/news/42";
    view.rerender(tree());
    await waitFor(() =>
      expect(
        screen.queryByRole("status", { name: "記事を読み込み中…" }),
      ).toBeNull(),
    );
  });
});
