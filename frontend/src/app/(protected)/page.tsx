import { Suspense } from "react";
import { CategorySidebar } from "@/components/layout/CategorySidebar";
import { MobileSidebar } from "@/components/layout/MobileSidebar";
import { NewsFilters } from "@/components/news/NewsFilters";
import { NewsList } from "@/components/news/NewsList";
import { NewsPagination } from "@/components/news/NewsPagination";
import { SearchBar } from "@/components/news/SearchBar";
import { getArticles, getCategories, getSources } from "@/lib/api-client";
import type { ArticleQuery, ImpactLevel } from "@/types";

interface DashboardPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

function parseSearchParams(
  raw: Record<string, string | string[] | undefined>,
): ArticleQuery {
  const query: ArticleQuery = {};
  const str = (key: string) => {
    const v = raw[key];
    return typeof v === "string" ? v : undefined;
  };

  const q = str("q");
  if (q) query.q = q;

  const keyword = str("keyword");
  if (keyword) query.keyword = keyword;

  const category = str("category");
  if (category) query.category = category;

  const impactLevel = str("impactLevel");
  if (
    impactLevel === "low" ||
    impactLevel === "medium" ||
    impactLevel === "high" ||
    impactLevel === "critical"
  ) {
    query.impactLevel = impactLevel as ImpactLevel;
  }

  const source = str("source");
  if (source) query.source = source;

  const sortBy = str("sortBy");
  if (sortBy === "publishedAt" || sortBy === "impactLevel") {
    query.sortBy = sortBy;
  }

  const sortOrder = str("sortOrder");
  if (sortOrder === "asc" || sortOrder === "desc") {
    query.sortOrder = sortOrder;
  }

  const page = str("page");
  if (page) query.page = Number(page);

  const perPage = str("perPage");
  if (perPage) query.perPage = Number(perPage);

  return query;
}

export default async function DashboardPage({
  searchParams,
}: DashboardPageProps) {
  const raw = await searchParams;
  const query = parseSearchParams(raw);

  const [newsData, categoriesData, sourcesData] = await Promise.all([
    getArticles(query),
    getCategories().catch(() => ({ items: [] })),
    getSources().catch(() => ({ items: [], total: 0 })),
  ]);

  return (
    <div className="flex h-full gap-0">
      {/* Sidebar */}
      <aside className="hidden lg:flex w-64 shrink-0 flex-col border-r border-border overflow-y-auto">
        <CategorySidebar
          categories={categoriesData.items}
          activeCategory={query.category}
          activeKeyword={query.keyword}
        />
      </aside>

      {/* Main content */}
      <main className="flex-1 min-w-0 flex flex-col overflow-y-auto">
        <div className="px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
          {/* Title row */}
          <div className="flex items-center gap-3">
            <MobileSidebar
              categories={categoriesData.items}
              activeCategory={query.category}
              activeKeyword={query.keyword}
            />
            <h1 className="text-base font-medium text-foreground">Dashboard</h1>
          </div>

          {/* Controls row: Search + Filters */}
          <div className="flex flex-col sm:flex-row sm:items-center gap-3">
            <Suspense>
              <SearchBar />
            </Suspense>
            <Suspense>
              <NewsFilters sources={sourcesData.items} />
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
