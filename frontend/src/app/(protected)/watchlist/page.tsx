import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { NewsList, NewsPagination } from "@/features/news";
import { getWatchlist } from "@/features/watchlist";
import { parseArticleQuery } from "@/lib/search-params/server";

export const metadata: Metadata = {
  title: "Watchlist | Vector",
};

interface WatchlistPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

async function WatchlistContent({ page }: { page: number }) {
  const data = await getWatchlist(page);

  if (data.items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
        <p className="text-sm font-medium">No saved articles</p>
        <p className="text-xs mt-1">
          Bookmark articles from the{" "}
          <Link href="/" className="underline">
            dashboard
          </Link>{" "}
          to see them here.
        </p>
      </div>
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
          <div className="h-4 w-20 rounded bg-muted/50 animate-pulse" />
          <div className="h-5 w-full rounded bg-muted/60 animate-pulse" />
          <div className="h-4 w-3/4 rounded bg-muted/40 animate-pulse" />
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
    <main className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
        <h1 className="text-base font-medium">Watchlist</h1>
        <Suspense key={page} fallback={<WatchlistSkeleton />}>
          <WatchlistContent page={page} />
        </Suspense>
      </div>
    </main>
  );
}
