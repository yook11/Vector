import { Suspense } from "react";
import {
  CategorySidebar,
  getArticles,
  getCategories,
  MobileSidebar,
  NewsFilters,
  NewsList,
  NewsPagination,
  parseArticleQuery,
  SearchBar,
  searchArticles,
} from "@/features/news";
import { getWatchlistIds } from "@/features/watchlist";
import type { ArticleQuery } from "@/types";

interface DashboardPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

async function CategorySidebarSection({
  activeCategory,
}: {
  activeCategory?: string;
}) {
  const { items } = await getCategories();
  return <CategorySidebar categories={items} activeCategory={activeCategory} />;
}

async function MobileSidebarTrigger({
  activeCategory,
}: {
  activeCategory?: string;
}) {
  const { items } = await getCategories();
  return <MobileSidebar categories={items} activeCategory={activeCategory} />;
}

async function NewsGridSection({
  filters,
  q,
}: {
  filters: ArticleQuery;
  q?: string;
}) {
  const fetchNews = q
    ? searchArticles({ q, ...filters })
    : getArticles(filters);
  const [newsData, watchedIds] = await Promise.all([
    fetchNews,
    getWatchlistIds(),
  ]);
  return (
    <>
      <NewsList items={newsData.items} watchedIds={watchedIds} />
      <NewsPagination page={newsData.page} totalPages={newsData.totalPages} />
    </>
  );
}

function CategorySidebarSkeleton() {
  return (
    <div className="flex flex-col gap-1.5 p-6" aria-hidden="true">
      <div className="h-4 w-24 rounded bg-muted/50 animate-pulse mb-2" />
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <div
          key={i}
          className="h-9 w-full rounded-xl bg-muted/40 animate-pulse"
        />
      ))}
    </div>
  );
}

function NewsGridSkeleton() {
  return (
    <div
      className="grid gap-x-8 gap-y-0 md:grid-cols-2 xl:grid-cols-3"
      aria-hidden="true"
    >
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <div
          key={i}
          className="flex flex-col gap-3 py-6 border-b border-border"
        >
          <div className="h-4 w-20 rounded bg-muted/50 animate-pulse" />
          <div className="h-5 w-full rounded bg-muted/60 animate-pulse" />
          <div className="h-4 w-3/4 rounded bg-muted/40 animate-pulse" />
        </div>
      ))}
    </div>
  );
}

export default async function DashboardPage({
  searchParams,
}: DashboardPageProps) {
  const raw = await searchParams;
  const { query: filters, q } = parseArticleQuery(raw);

  return (
    <div className="flex h-full gap-0">
      {/* Sidebar */}
      <aside className="hidden lg:flex w-64 shrink-0 flex-col border-r border-border overflow-y-auto">
        <Suspense fallback={<CategorySidebarSkeleton />}>
          <CategorySidebarSection activeCategory={filters.category} />
        </Suspense>
      </aside>

      {/* Main content */}
      <main className="flex-1 min-w-0 flex flex-col overflow-y-auto">
        <div className="px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
          {/* Title row */}
          <div className="flex items-center gap-3">
            <Suspense fallback={null}>
              <MobileSidebarTrigger activeCategory={filters.category} />
            </Suspense>
            <h1 className="text-base font-medium text-foreground">Dashboard</h1>
          </div>

          {/* Controls row: Search + Filters */}
          <div className="flex flex-col sm:flex-row sm:items-center gap-3">
            <Suspense>
              <SearchBar />
            </Suspense>
            <Suspense>
              <NewsFilters />
            </Suspense>
          </div>

          {/* News grid */}
          <Suspense
            key={JSON.stringify({ q, filters })}
            fallback={<NewsGridSkeleton />}
          >
            <NewsGridSection filters={filters} q={q} />
          </Suspense>
        </div>
      </main>
    </div>
  );
}
