import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DashboardPage from "./page";

type Deferred = {
  promise: Promise<unknown>;
  resolve: () => void;
};

const mocks = vi.hoisted(() => ({
  getArticles: vi.fn(),
  getCategories: vi.fn(),
  getWatchlistIds: vi.fn(),
  parseArticleQuery: vi.fn(),
  requireSession: vi.fn(),
}));

vi.mock("@/components/layout/nav-items", () => ({
  getProtectedNavItems: vi.fn().mockReturnValue([]),
}));

vi.mock("@/components/layout/PageNavigation", () => ({
  PageNavigationContent: () => null,
}));

vi.mock("@/components/layout/ThemeToggle", () => ({
  ThemeToggle: () => null,
}));

vi.mock("@/components/paper", () => ({
  formatPaperMastheadDate: vi.fn(),
  PaperSurface: () => null,
  PaperTexture: () => null,
}));

vi.mock("@/features/auth", () => ({
  UserMenu: () => null,
}));

vi.mock("@/features/news", () => ({
  DashboardArticleListSkeleton: () => null,
  DashboardMasthead: () => null,
  DashboardPaperArticleList: () => null,
  getArticles: mocks.getArticles,
  getCategories: mocks.getCategories,
  getLatestArticleDate: vi.fn(),
  PaperNewsControls: () => null,
  PaperNewsPagination: () => null,
  PaperNewsResultSummary: () => null,
  parseArticleQuery: mocks.parseArticleQuery,
}));

vi.mock("@/features/watchlist", () => ({
  getWatchlistIds: mocks.getWatchlistIds,
}));

vi.mock("@/lib/auth/guards", () => ({
  requireSession: mocks.requireSession,
}));

vi.mock("@/lib/auth/role", () => ({
  narrowRole: vi.fn().mockReturnValue("member"),
}));

function createDeferred(): Deferred {
  let resolvePromise: (() => void) | undefined;
  const promise = new Promise<unknown>((resolve) => {
    resolvePromise = () => resolve({});
  });
  return {
    promise,
    resolve: () => resolvePromise?.(),
  };
}

function settlesBeforeNextTask(promise: Promise<unknown>): Promise<boolean> {
  return Promise.race([
    promise.then(() => true),
    new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 0)),
  ]);
}

let categories: Deferred;
let articles: Deferred;
let watchlistIds: Deferred;

beforeEach(() => {
  categories = createDeferred();
  articles = createDeferred();
  watchlistIds = createDeferred();

  mocks.getCategories.mockReset().mockReturnValue(categories.promise);
  mocks.getArticles.mockReset().mockReturnValue(articles.promise);
  mocks.getWatchlistIds.mockReset().mockReturnValue(watchlistIds.promise);
  mocks.parseArticleQuery.mockReset().mockReturnValue({ query: {} });
  mocks.requireSession
    .mockReset()
    .mockResolvedValue({ user: { role: "user" } });
});

afterEach(() => {
  categories.resolve();
  articles.resolve();
  watchlistIds.resolve();
});

describe("Dashboard initial loading shell", () => {
  it("starts independent data requests and returns its outer tree before categories resolve", async () => {
    const outerTree = DashboardPage({ searchParams: Promise.resolve({}) });
    const settled = await settlesBeforeNextTask(outerTree);

    expect.soft(settled).toBe(true);
    expect.soft(mocks.getCategories).toHaveBeenCalledTimes(1);
    expect.soft(mocks.getArticles).toHaveBeenCalledTimes(1);
    expect.soft(mocks.getWatchlistIds).toHaveBeenCalledTimes(1);
  });
});
