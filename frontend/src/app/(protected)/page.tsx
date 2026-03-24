import { Suspense } from "react";
import { CategorySidebar } from "@/components/layout/CategorySidebar";
import { MobileSidebar } from "@/components/layout/MobileSidebar";
import { NewsFilters } from "@/components/news/NewsFilters";
import { NewsList } from "@/components/news/NewsList";
import { NewsPagination } from "@/components/news/NewsPagination";
import { SearchBar } from "@/components/news/SearchBar";
import { getCategories, getNews, getSources } from "@/lib/api-client";
import type { NewsQuery, Sentiment } from "@/types";

interface DashboardPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

function parseSearchParams(
  raw: Record<string, string | string[] | undefined>,
): NewsQuery {
  const query: NewsQuery = {};
  const str = (key: string) => {
    const v = raw[key];
    return typeof v === "string" ? v : undefined;
  };

  const q = str("q");
  if (q) query.q = q;

  const keywordId = str("keywordId");
  if (keywordId) query.keywordId = Number(keywordId);

  const kwCategoryId = str("kwCategoryId");
  if (kwCategoryId) query.kwCategoryId = Number(kwCategoryId);

  const sentiment = str("sentiment");
  if (
    sentiment === "positive" ||
    sentiment === "negative" ||
    sentiment === "neutral"
  ) {
    query.sentiment = sentiment as Sentiment;
  }

  const minImpact = str("minImpact");
  if (minImpact) query.minImpact = Number(minImpact);

  const sourceId = str("sourceId");
  if (sourceId) query.sourceId = Number(sourceId);

  const sortBy = str("sortBy");
  if (sortBy === "publishedAt" || sortBy === "impactScore") {
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
    getNews(query),
    getCategories().catch(() => ({ items: [] })),
    getSources().catch(() => ({ items: [], total: 0 })),
  ]);

  return (
    <div className="flex h-full">
      <aside className="hidden lg:block w-64 border-r overflow-y-auto">
        <CategorySidebar
          categories={categoriesData.items}
          activeKwCategoryId={query.kwCategoryId}
          activeKeywordId={query.keywordId}
        />
      </aside>

      <main className="flex-1 p-6 space-y-6 overflow-y-auto">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <MobileSidebar
              categories={categoriesData.items}
              activeKwCategoryId={query.kwCategoryId}
              activeKeywordId={query.keywordId}
            />
            <h1 className="text-2xl font-bold">Dashboard</h1>
          </div>
        </div>

        <Suspense>
          <SearchBar />
        </Suspense>

        <Suspense>
          <NewsFilters sources={sourcesData.items} />
        </Suspense>

        <NewsList items={newsData.items} />

        <NewsPagination page={newsData.page} totalPages={newsData.totalPages} />
      </main>
    </div>
  );
}
