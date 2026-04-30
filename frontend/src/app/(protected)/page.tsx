import { Suspense } from "react";
import { Skeleton } from "@/components/ui/skeleton";
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
import type { SearchParams } from "@/lib/types/route";
import type { ArticleQuery } from "@/types";

interface DashboardPageProps {
  searchParams: Promise<SearchParams>;
}

// `getCategories` は `'use cache'` を持つため、CategorySidebarSection と
// MobileSidebarTrigger の 2 箇所で await しても Next.js 16 の cache hit で
// 実 backend hit は 1 回に収束する。Suspense 境界を別にすることで lg/mobile
// 両方が独立に streaming される。
async function CategorySidebarSection(props: { activeCategory?: string }) {
  const { items } = await getCategories();
  // EOP 下では undefined 明示代入が違反になるため、props を spread して
  // optional の不在/存在をそのまま伝搬する。
  return <CategorySidebar categories={items} {...props} />;
}

async function MobileSidebarTrigger(props: { activeCategory?: string }) {
  const { items } = await getCategories();
  return <MobileSidebar categories={items} {...props} />;
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
      <Skeleton className="h-4 w-24 mb-2" />
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <Skeleton key={i} className="h-9 w-full rounded-xl" />
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
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-5 w-full" />
          <Skeleton className="h-4 w-3/4" />
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
  // EOP 下で undefined を optional prop に明示代入できないため、
  // 条件付き spread で「未指定 or 値あり」を表現する。
  const categoryProps =
    filters.category !== undefined ? { activeCategory: filters.category } : {};
  const qProps = q !== undefined ? { q } : {};

  return (
    <div className="flex h-full gap-0">
      {/* Sidebar */}
      <aside className="hidden lg:flex w-64 shrink-0 flex-col border-r border-border overflow-y-auto">
        <Suspense fallback={<CategorySidebarSkeleton />}>
          <CategorySidebarSection {...categoryProps} />
        </Suspense>
      </aside>

      {/* Main content */}
      <main className="flex-1 min-w-0 flex flex-col overflow-y-auto">
        <div className="px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
          {/* Title row */}
          <div className="flex items-center gap-3">
            <Suspense fallback={null}>
              <MobileSidebarTrigger {...categoryProps} />
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
            <NewsGridSection filters={filters} {...qProps} />
          </Suspense>
        </div>
      </main>
    </div>
  );
}
