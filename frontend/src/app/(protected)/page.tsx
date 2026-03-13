import { Suspense } from "react";
import { getCategories, getKeywordCategories, getNews, getSources, getSubscriptions } from "@/lib/api-client";
import { NewsList } from "@/components/news/NewsList";
import { NewsFilters } from "@/components/news/NewsFilters";
import { NewsPagination } from "@/components/news/NewsPagination";
import { SearchBar } from "@/components/news/SearchBar";
import { CategorySidebar } from "@/components/layout/CategorySidebar";
import { MobileSidebar } from "@/components/layout/MobileSidebar";
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

  const myKeywords = str("myKeywords");
  if (myKeywords === "true") query.myKeywords = true;

  const sentiment = str("sentiment");
  if (sentiment === "positive" || sentiment === "negative" || sentiment === "neutral") {
    query.sentiment = sentiment as Sentiment;
  }

  const minImpact = str("minImpact");
  if (minImpact) query.minImpact = Number(minImpact);

  const sourceId = str("sourceId");
  if (sourceId) query.sourceId = Number(sourceId);

  const category = str("category");
  if (category) query.category = category;

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

export default async function DashboardPage({ searchParams }: DashboardPageProps) {
  const raw = await searchParams;
  const query = parseSearchParams(raw);

  const [newsData, subscriptionsData, categoriesData, kwCategoriesData, sourcesData] = await Promise.all([
    getNews(query),
    getSubscriptions().catch(() => ({ items: [] })),
    getCategories().catch(() => ({ items: [] })),
    getKeywordCategories().catch(() => ({ items: [] })),
    getSources().catch(() => ({ items: [], total: 0 })),
  ]);

  const subscribedKeywordIds = subscriptionsData.items.map((s) => s.keywordId);

  return (
    <div className="flex">
      <aside className="hidden lg:block w-64 border-r min-h-[calc(100vh-3.5rem)]">
        <CategorySidebar
          categories={kwCategoriesData.items}
          activeKwCategoryId={query.kwCategoryId}
          activeKeywordId={query.keywordId}
          subscribedKeywordIds={subscribedKeywordIds}
          showMyKeywords={query.myKeywords}
        />
      </aside>

      <main className="flex-1 p-6 space-y-6">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <MobileSidebar
              categories={kwCategoriesData.items}
              activeKwCategoryId={query.kwCategoryId}
              activeKeywordId={query.keywordId}
              subscribedKeywordIds={subscribedKeywordIds}
              showMyKeywords={query.myKeywords}
            />
            <h1 className="text-2xl font-bold">Dashboard</h1>
          </div>
        </div>

        <Suspense>
          <SearchBar />
        </Suspense>

        <Suspense>
          <NewsFilters categories={categoriesData.items} sources={sourcesData.items} />
        </Suspense>

        <NewsList items={newsData.items} />

        <NewsPagination
          page={newsData.page}
          totalPages={newsData.totalPages}
        />
      </main>
    </div>
  );
}
