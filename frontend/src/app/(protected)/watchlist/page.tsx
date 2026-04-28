import type { Metadata } from "next";
import Link from "next/link";
import { NewsList, NewsPagination } from "@/features/news";
import { getWatchlist } from "@/features/watchlist";
import { parseArticleQuery } from "@/lib/search-params/server";

export const metadata: Metadata = {
  title: "Watchlist | Vector",
};

interface WatchlistPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function WatchlistPage({
  searchParams,
}: WatchlistPageProps) {
  const raw = await searchParams;
  const { query } = parseArticleQuery(raw);
  const page = query.page ?? 1;

  const data = await getWatchlist(page);

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
        <h1 className="text-base font-medium">Watchlist</h1>

        {data.items.length === 0 ? (
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
        ) : (
          <NewsList items={data.items} />
        )}

        <NewsPagination page={data.page} totalPages={data.totalPages} />
      </div>
    </main>
  );
}
