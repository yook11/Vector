import { Suspense } from "react";
import { CategorySidebar } from "@/components/layout/CategorySidebar";
import { MobileSidebar } from "@/components/layout/MobileSidebar";
import {
  getArticles,
  getCategories,
  NewsFilters,
  NewsList,
  NewsPagination,
  SearchBar,
  searchArticles,
} from "@/features/news";
import { parseArticleQuery } from "@/lib/search-params/server";

interface DashboardPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function DashboardPage({
  searchParams,
}: DashboardPageProps) {
  const raw = await searchParams;
  const { query: filters, q } = parseArticleQuery(raw);

  const fetchNews = q
    ? searchArticles({ q, ...filters })
    : getArticles(filters);

  const [newsData, categoriesData] = await Promise.all([
    fetchNews,
    getCategories().catch(() => ({ items: [] })),
  ]);

  return (
    <div className="flex h-full gap-0">
      {/* Sidebar */}
      <aside className="hidden lg:flex w-64 shrink-0 flex-col border-r border-border overflow-y-auto">
        <CategorySidebar
          categories={categoriesData.items}
          activeCategory={filters.category}
        />
      </aside>

      {/* Main content */}
      <main className="flex-1 min-w-0 flex flex-col overflow-y-auto">
        <div className="px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
          {/* Title row */}
          <div className="flex items-center gap-3">
            <MobileSidebar
              categories={categoriesData.items}
              activeCategory={filters.category}
            />
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
          <NewsList items={newsData.items} />

          <NewsPagination
            page={newsData.page}
            totalPages={newsData.totalPages}
          />
        </div>
      </main>
    </div>
  );
}
