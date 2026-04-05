import type { Metadata } from "next";
import Link from "next/link";
import { NewsList } from "@/components/news/NewsList";
import { getWatchlist } from "@/lib/api-client";

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
  const page = typeof raw.page === "string" ? Number(raw.page) : 1;

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

        {data.totalPages > 1 && (
          <div className="flex justify-center gap-2">
            {page > 1 && (
              <Link
                href={`/watchlist?page=${page - 1}`}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                Previous
              </Link>
            )}
            <span className="text-xs text-muted-foreground tabular-nums">
              Page {data.page} of {data.totalPages}
            </span>
            {page < data.totalPages && (
              <Link
                href={`/watchlist?page=${page + 1}`}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                Next
              </Link>
            )}
          </div>
        )}
      </div>
    </main>
  );
}
