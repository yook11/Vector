import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  AnchorHTMLAttributes,
  ComponentProps,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  pathname: "/",
  pendingByHref: new Map<string, boolean>(),
  pendingBySource: new Map<number, boolean>(),
  nextLinkSource: 0,
  linkClicks: [] as { defaultPrevented: boolean; href: string }[],
  requireSession: vi.fn(),
  session: { data: null as { user: { role: string } } | null },
}));

type LinkMockProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  children: ReactNode;
  href: string;
  onNavigate?: (event: { preventDefault: () => void }) => void;
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
    onClick,
    onNavigate,
    prefetch: _prefetch,
    replace: _replace,
    scroll: _scroll,
    target,
    download,
    ...rest
  }: LinkMockProps) {
    const sourceRef = React.useRef<number | null>(null);
    if (sourceRef.current === null) {
      sourceRef.current = mocks.nextLinkSource;
      mocks.nextLinkSource += 1;
    }
    const source = sourceRef.current;
    const [clickedPending, setClickedPending] = React.useState(false);
    const external =
      new URL(href, "http://vector.test").origin !== "http://vector.test";
    const pending =
      mocks.pendingBySource.get(source) ??
      mocks.pendingByHref.get(href) ??
      clickedPending;

    function handleClick(event: ReactMouseEvent<HTMLAnchorElement>) {
      onClick?.(event);
      const browserManaged =
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey ||
        external ||
        target === "_blank" ||
        download !== undefined;
      if (
        !browserManaged &&
        !event.defaultPrevented &&
        onNavigate !== undefined
      ) {
        onNavigate({ preventDefault: () => event.preventDefault() });
      }
      const defaultPrevented = event.defaultPrevented;
      mocks.linkClicks.push({ defaultPrevented, href });
      if (!browserManaged && !defaultPrevented) setClickedPending(true);
      // JSDOM は実document navigationを実装しないため、観測後にだけ止める。
      event.preventDefault();
    }

    return (
      <PendingContext.Provider value={pending}>
        <a
          data-next-link-source={source}
          download={download}
          href={href}
          onClick={handleClick}
          target={target}
          {...rest}
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

vi.mock("@/lib/auth/guards", () => ({
  requireSession: () => mocks.requireSession(),
}));

vi.mock("@/lib/auth/auth-client", () => ({
  useSession: () => mocks.session,
}));

vi.mock("@/components/layout/ThemeToggle", () => ({
  ThemeToggle: () => <button type="button">テーマ</button>,
}));

vi.mock("@/components/layout/ShellMobileNav", () => ({
  ShellMobileNav: () => null,
}));

vi.mock("@/features/auth", () => ({
  UserMenu: () => null,
}));

import { NavLink } from "@/components/layout/NavLink";
import { ShellNav } from "@/components/layout/ShellNav";
import ShellMainLayout from "./(shell)/(main)/layout";
import ProtectedError from "./error";
import ProtectedLayout from "./layout";
import NewsDetailError from "./news/[id]/error";
import NewsNotFound from "./news/[id]/not-found";
import ProtectedNotFound from "./not-found";

async function renderProtectedPage() {
  const tree = await ProtectedLayout({
    children: (
      <ShellMainLayout>
        <main data-testid="current-page-content">
          <h1>遷移前の本文</h1>
        </main>
      </ShellMainLayout>
    ),
  });
  return render(tree);
}

async function protectedRouteTree(route: ReactNode) {
  return ProtectedLayout({
    children: (
      <>
        <ShellNav />
        {route}
      </>
    ),
  });
}

async function protectedRouteTreeWithArticleLink(route: ReactNode) {
  return ProtectedLayout({
    children: (
      <>
        <NavLink href="/news/101" pendingAware>
          記事詳細
        </NavLink>
        {route}
      </>
    ),
  });
}

async function duplicateShellNavTree() {
  return ProtectedLayout({
    children: (
      <>
        <ShellNav />
        <ShellNav />
      </>
    ),
  });
}

async function navLinkTree(props: ComponentProps<typeof NavLink>) {
  return ProtectedLayout({ children: <NavLink {...props} /> });
}

function sourceId(link: HTMLElement): number {
  const value = link.getAttribute("data-next-link-source");
  expect(value).not.toBeNull();
  return Number(value);
}

function globalStatus(): HTMLElement | null {
  return screen.queryByRole("status", { name: /を読み込み中…/ });
}

describe("protected page navigation feedback", () => {
  beforeEach(() => {
    mocks.pathname = "/";
    mocks.pendingByHref.clear();
    mocks.pendingBySource.clear();
    mocks.nextLinkSource = 0;
    mocks.linkClicks.length = 0;
    mocks.requireSession.mockReset();
    mocks.requireSession.mockResolvedValue({ user: { id: "user-1" } });
    mocks.session = { data: null };
  });

  it("latest pending Link だけがtarget別statusと現在contentのoverlayを所有する", async () => {
    const view = await renderProtectedPage();
    const content = screen.getByTestId("current-page-content");
    const masthead = screen.getByRole("banner");

    expect(globalStatus()).toBeNull();
    expect(content.closest("[aria-busy='true']")).toBeNull();

    mocks.pendingByHref.set("/research", true);
    view.rerender(
      await ProtectedLayout({
        children: (
          <ShellMainLayout>
            <main data-testid="current-page-content">
              <h1>遷移前の本文</h1>
            </main>
          </ShellMainLayout>
        ),
      }),
    );

    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Researchを読み込み中…"),
    );
    expect(content).toBeInTheDocument();
    expect(content.closest("[aria-busy='true']")).not.toBeNull();
    expect(screen.getByTestId("page-navigation-overlay")).toBeInTheDocument();
    expect(masthead.closest("[aria-busy='true']")).toBeNull();

    mocks.pendingByHref.set("/briefing", true);
    view.rerender(
      await ProtectedLayout({
        children: (
          <ShellMainLayout>
            <main data-testid="current-page-content">
              <h1>遷移前の本文</h1>
            </main>
          </ShellMainLayout>
        ),
      }),
    );

    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Briefingを読み込み中…"),
    );

    mocks.pendingByHref.set("/research", false);
    view.rerender(
      await ProtectedLayout({
        children: (
          <ShellMainLayout>
            <main data-testid="current-page-content">
              <h1>遷移前の本文</h1>
            </main>
          </ShellMainLayout>
        ),
      }),
    );

    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Briefingを読み込み中…"),
    );

    mocks.pendingByHref.set("/briefing", false);
    view.rerender(
      await ProtectedLayout({
        children: (
          <ShellMainLayout>
            <main data-testid="current-page-content">
              <h1>遷移前の本文</h1>
            </main>
          </ShellMainLayout>
        ),
      }),
    );

    await waitFor(() => expect(globalStatus()).toBeNull());
    expect(content.closest("[aria-busy='true']")).toBeNull();

    mocks.pathname = "/";
    mocks.pendingByHref.set("/research", true);
    view.rerender(
      await ProtectedLayout({
        children: (
          <ShellMainLayout>
            <main data-testid="current-page-content">
              <h1>遷移前の本文</h1>
            </main>
          </ShellMainLayout>
        ),
      }),
    );
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Researchを読み込み中…"),
    );

    mocks.pathname = "/research?from=dashboard";
    view.rerender(
      await ProtectedLayout({
        children: (
          <ShellMainLayout>
            <main data-testid="current-page-content">
              <h1>Research本文</h1>
            </main>
          </ShellMainLayout>
        ),
      }),
    );
    await waitFor(() => expect(globalStatus()).toBeNull());
  });

  it("committed not-found UIがmountしたら残ったglobal pendingを解除する", async () => {
    const view = render(await protectedRouteTree(<p>遷移前の本文</p>));

    mocks.pendingByHref.set("/research", true);
    view.rerender(await protectedRouteTree(<p>遷移前の本文</p>));
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Researchを読み込み中…"),
    );

    view.rerender(await protectedRouteTree(<ProtectedNotFound />));

    await waitFor(() => expect(globalStatus()).toBeNull());
  });

  it("committed route error UIがmountしたら残ったglobal pendingを解除する", async () => {
    const view = render(await protectedRouteTree(<p>遷移前の本文</p>));

    mocks.pendingByHref.set("/research", true);
    view.rerender(await protectedRouteTree(<p>遷移前の本文</p>));
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Researchを読み込み中…"),
    );

    view.rerender(
      await protectedRouteTree(
        <ProtectedError
          error={new Error("route failure")}
          reset={() => undefined}
          unstable_retry={() => undefined}
        />,
      ),
    );

    await waitFor(() => expect(globalStatus()).toBeNull());
  });

  it("child route固有error UIがmountしたら残ったglobal pendingを解除する", async () => {
    const view = render(
      await protectedRouteTreeWithArticleLink(<p>遷移前の記事一覧</p>),
    );

    mocks.pendingByHref.set("/news/101", true);
    view.rerender(
      await protectedRouteTreeWithArticleLink(<p>遷移前の記事一覧</p>),
    );
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("記事を読み込み中…"),
    );

    view.rerender(
      await protectedRouteTreeWithArticleLink(
        <NewsDetailError
          error={new Error("article route failure")}
          reset={() => undefined}
          unstable_retry={() => undefined}
        />,
      ),
    );

    await waitFor(() => expect(globalStatus()).toBeNull());
  });

  it("URL差分なしでchild route固有not-found UIがmountしても残ったglobal pendingを解除する", async () => {
    const view = render(
      await protectedRouteTreeWithArticleLink(<p>遷移前の記事一覧</p>),
    );

    mocks.pendingByHref.set("/news/101", true);
    view.rerender(
      await protectedRouteTreeWithArticleLink(<p>遷移前の記事一覧</p>),
    );
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("記事を読み込み中…"),
    );
    expect(mocks.pathname).toBe("/");

    view.rerender(await protectedRouteTreeWithArticleLink(<NewsNotFound />));

    await waitFor(() => expect(globalStatus()).toBeNull());
    expect(screen.getByText("Article not found.")).toBeInTheDocument();
    expect(mocks.pathname).toBe("/");
  });

  it("同一hrefでもcurrent sourceのfalling edgeだけがpendingを解除する", async () => {
    const view = render(await duplicateShellNavTree());
    const [firstResearch, secondResearch] = screen.getAllByRole("link", {
      name: "Research",
    });
    expect(firstResearch).toBeDefined();
    expect(secondResearch).toBeDefined();
    if (firstResearch === undefined || secondResearch === undefined) {
      throw new Error("Research links must be rendered twice");
    }
    const firstSource = sourceId(firstResearch);
    const secondSource = sourceId(secondResearch);

    expect(firstSource).not.toBe(secondSource);
    expect(globalStatus()).toBeNull();

    mocks.pendingBySource.set(secondSource, false);
    view.rerender(await duplicateShellNavTree());
    await waitFor(() => expect(globalStatus()).toBeNull());

    mocks.pendingBySource.set(firstSource, true);
    view.rerender(await duplicateShellNavTree());
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Researchを読み込み中…"),
    );

    mocks.pendingBySource.set(secondSource, false);
    view.rerender(await duplicateShellNavTree());
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Researchを読み込み中…"),
    );

    mocks.pendingBySource.set(secondSource, true);
    view.rerender(await duplicateShellNavTree());
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Researchを読み込み中…"),
    );

    mocks.pendingBySource.set(firstSource, false);
    view.rerender(await duplicateShellNavTree());
    await waitFor(() =>
      expect(globalStatus()).toHaveTextContent("Researchを読み込み中…"),
    );

    mocks.pendingBySource.set(secondSource, false);
    view.rerender(await duplicateShellNavTree());
    await waitFor(() => expect(globalStatus()).toBeNull());
  });

  it.each([
    ["Cmd click", { metaKey: true }],
    ["Ctrl click", { ctrlKey: true }],
    ["Shift click", { shiftKey: true }],
    ["Alt click", { altKey: true }],
    ["middle click", { button: 1 }],
  ])("%sはnative defaultを維持してglobal pendingを開始しない", async (_name, event) => {
    render(
      await navLinkTree({
        children: "Briefing",
        href: "/briefing",
      }),
    );

    fireEvent.click(screen.getByRole("link", { name: "Briefing" }), event);

    expect(mocks.linkClicks).toEqual([
      { defaultPrevented: false, href: "/briefing" },
    ]);
    expect(globalStatus()).toBeNull();
  });

  it.each([
    [
      "external URL",
      { children: "外部", href: "https://example.com/report" },
      "外部",
    ],
    [
      "target=_blank",
      { children: "別tab", href: "/briefing", target: "_blank" },
      "別tab",
    ],
    [
      "download",
      { children: "ダウンロード", download: "report.pdf", href: "/report.pdf" },
      "ダウンロード",
    ],
  ] as const)("%sはnative defaultを維持してglobal pendingを開始しない", async (_name, props, label) => {
    render(await navLinkTree(props));

    fireEvent.click(screen.getByRole("link", { name: label }));

    expect(mocks.linkClicks).toEqual([
      { defaultPrevented: false, href: props.href },
    ]);
    expect(globalStatus()).toBeNull();
  });
});
