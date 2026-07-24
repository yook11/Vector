import { render, screen, waitFor } from "@testing-library/react";
import {
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getArticles: vi.fn(),
  getCategories: vi.fn(),
  getWatchlistIds: vi.fn(),
  requireSession: vi.fn(),
}));

vi.mock("@/components/layout/nav-items", () => ({
  getProtectedNavItems: () => [{ href: "/", label: "ニュース", icon: "news" }],
}));

vi.mock("@/components/layout/PageNavigation", () => ({
  PageNavigationContent: ({ children }: { children: ReactNode }) => (
    <div aria-busy="true" data-testid="dashboard-navigation-outlet">
      {children}
      <div data-testid="page-navigation-overlay" />
    </div>
  ),
}));

vi.mock("@/components/layout/ThemeToggle", () => ({
  ThemeToggle: () => <button type="button">テーマ</button>,
}));

vi.mock("@/components/paper", () => ({
  formatPaperMastheadDate: () => "2026年7月24日",
  PaperSurface: ({ children }: { children: ReactNode }) => (
    <div data-testid="paper-surface">{children}</div>
  ),
  PaperTexture: () => <div data-testid="paper-texture" />,
}));

vi.mock("@/features/auth", () => ({
  UserMenu: () => <button type="button">ユーザーメニュー</button>,
}));

vi.mock("@/features/news", () => ({
  DashboardArticleListSkeleton: () => <p>記事を更新中…</p>,
  DashboardMasthead: ({
    themeSlot,
    userMenuSlot,
  }: {
    themeSlot: ReactNode;
    userMenuSlot: ReactNode;
  }) => (
    <header data-testid="dashboard-masthead">
      <nav data-testid="dashboard-primary-nav">主要ページ</nav>
      {themeSlot}
      {userMenuSlot}
    </header>
  ),
  DashboardPaperArticleList: () => (
    <section data-testid="dashboard-article-results">記事結果</section>
  ),
  getArticles: mocks.getArticles,
  getCategories: mocks.getCategories,
  getLatestArticleDate: () => null,
  PaperNewsControls: () => <button type="button">表示設定</button>,
  PaperNewsPagination: () => <nav>記事ページ</nav>,
  PaperNewsResultSummary: () => (
    <p data-testid="dashboard-result-summary">検索結果</p>
  ),
  parseArticleQuery: () => ({ query: {} }),
}));

vi.mock("@/features/watchlist", () => ({
  getWatchlistIds: mocks.getWatchlistIds,
}));

vi.mock("@/lib/auth/guards", () => ({
  requireSession: mocks.requireSession,
}));

vi.mock("@/lib/auth/role", () => ({
  narrowRole: () => "member",
}));

import DashboardPage from "./page";

async function resolveServerTree(node: ReactNode): Promise<ReactNode> {
  if (Array.isArray(node)) {
    return Promise.all(node.map((child) => resolveServerTree(child)));
  }
  if (!isValidElement(node)) return node;

  if (typeof node.type === "function") {
    const component = node.type as (props: unknown) => ReactNode;
    return resolveServerTree(await component(node.props));
  }

  const element = node as ReactElement<{ children?: ReactNode }>;
  const children = await resolveServerTree(element.props.children);
  return cloneElement(element, undefined, children);
}

describe("Dashboard page navigation outlet", () => {
  it("masthead controlsをbusy overlay外に保ち、result outletだけを内包する", async () => {
    mocks.requireSession.mockResolvedValue({ user: { role: "user" } });
    mocks.getCategories.mockResolvedValue({ items: [] });
    mocks.getArticles.mockResolvedValue({
      items: [],
      page: 1,
      totalPages: 1,
    });
    mocks.getWatchlistIds.mockResolvedValue([]);

    const page = await DashboardPage({
      searchParams: Promise.resolve({}),
    });
    render(await resolveServerTree(page));

    const outlet = screen.getByTestId("dashboard-navigation-outlet");
    const masthead = await screen.findByTestId("dashboard-masthead");
    const nav = screen.getByTestId("dashboard-primary-nav");
    const theme = screen.getByRole("button", { name: "テーマ" });
    const userMenu = screen.getByRole("button", {
      name: "ユーザーメニュー",
    });
    await waitFor(() =>
      expect(screen.getByTestId("dashboard-article-results")).toBeVisible(),
    );

    for (const persistentControl of [masthead, nav, theme, userMenu]) {
      expect.soft(outlet).not.toContainElement(persistentControl);
      expect.soft(persistentControl.closest("[aria-busy='true']")).toBeNull();
    }
    expect(outlet).toContainElement(
      screen.getByTestId("dashboard-result-summary"),
    );
    expect(outlet).toContainElement(
      screen.getByTestId("dashboard-article-results"),
    );
    expect(outlet).toContainElement(
      screen.getByTestId("page-navigation-overlay"),
    );
  });
});
