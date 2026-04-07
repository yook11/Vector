import { Suspense } from "react";
import { CategorySidebar } from "@/components/layout/CategorySidebar";
import { MobileSidebar } from "@/components/layout/MobileSidebar";
import { NewsFilters } from "@/components/news/NewsFilters";
import { NewsList } from "@/components/news/NewsList";
import { NewsPagination } from "@/components/news/NewsPagination";
import { SearchBar } from "@/components/news/SearchBar";
import {
  getArticles,
  getCategories,
  getSources,
  searchArticles,
} from "@/lib/api-client";
import type { ImpactLevel } from "@/types";

interface DashboardPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

function parseCommonFilters(
  raw: Record<string, string | string[] | undefined>,
) {
  const str = (key: string) => {
    const v = raw[key];
    return typeof v === "string" ? v : undefined;
  };

  const filters: {
    keyword?: string;
    category?: string;
    impactLevel?: ImpactLevel;
    source?: string;
    sortOrder?: "asc" | "desc";
    page?: number;
    perPage?: number;
  } = {};

  const keyword = str("keyword");
  if (keyword) filters.keyword = keyword;

  const category = str("category");
  if (category) filters.category = category;

  const impactLevel = str("impactLevel");
  if (
    impactLevel === "low" ||
    impactLevel === "medium" ||
    impactLevel === "high" ||
    impactLevel === "critical"
  ) {
    filters.impactLevel = impactLevel as ImpactLevel;
  }

  const source = str("source");
  if (source) filters.source = source;

  const sortOrder = str("sortOrder");
  if (sortOrder === "asc" || sortOrder === "desc") {
    filters.sortOrder = sortOrder;
  }

  const page = str("page");
  if (page) filters.page = Number(page);

  const perPage = str("perPage");
  if (perPage) filters.perPage = Number(perPage);

  return { filters, q: str("q") };
}

export default async function DashboardPage({
  searchParams,
}: DashboardPageProps) {
  const raw = await searchParams;
  const { filters, q } = parseCommonFilters(raw);

  const fetchNews = q
    ? searchArticles({ q, ...filters })
    : getArticles(filters);

  const [newsData, categoriesData, sourcesData] = await Promise.all([
    fetchNews,
    getCategories().catch(() => ({ items: [] })),
    getSources().catch(() => ({ items: [], total: 0 })),
  ]);

  return (
    <div className="flex h-full gap-0">
      {/* Sidebar */}
      <aside className="hidden lg:flex w-64 shrink-0 flex-col border-r border-border overflow-y-auto">
        <CategorySidebar
          categories={categoriesData.items}
          activeCategory={filters.category}
          activeKeyword={filters.keyword}
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
              activeKeyword={filters.keyword}
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
