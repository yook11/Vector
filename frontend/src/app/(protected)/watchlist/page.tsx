import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { EmptyState } from "@/components/feedback/EmptyState";
import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";
import { NewsList, NewsPagination, parseArticleQuery } from "@/features/news";
import { getWatchlist } from "@/features/watchlist";
import type { SearchParams } from "@/lib/types/route";

export const metadata: Metadata = {
  title: "Watchlist | Vector",
};

interface WatchlistPageProps {
  searchParams: Promise<SearchParams>;
}

async function WatchlistContent({ page }: { page: number }) {
  const data = await getWatchlist(page);

  if (data.items.length === 0) {
    return (
      <EmptyState
        title="No saved articles"
        description={
          <>
            Bookmark articles from the{" "}
            <Link href="/" className="underline">
              dashboard
            </Link>{" "}
            to see them here.
          </>
        }
      />
    );
  }

  // /watchlist 配下の記事は全件 watched が定義上自明なので、追加 fetch を
  // せず item ID から直接 Set を作る。
  const watchedIds = new Set(data.items.map((a) => a.id));

  return (
    <>
      <NewsList items={data.items} watchedIds={watchedIds} />
      <NewsPagination page={data.page} totalPages={data.totalPages} />
    </>
  );
}

function WatchlistSkeleton() {
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

export default async function WatchlistPage({
  searchParams,
}: WatchlistPageProps) {
  const raw = await searchParams;
  const { query } = parseArticleQuery(raw);
  const page = query.page ?? 1;

  return (
    <PageContainer>
      <h1 className="text-base font-medium">Watchlist</h1>
      <Suspense key={page} fallback={<WatchlistSkeleton />}>
        <WatchlistContent page={page} />
      </Suspense>
    </PageContainer>
  );
}
