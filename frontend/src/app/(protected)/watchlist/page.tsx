import type { Metadata } from "next";
import Link from "next/link";
import { WatchlistButton } from "@/components/news/WatchlistButton";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getWatchlist } from "@/lib/api-client";

export const metadata: Metadata = {
  title: "Watchlist | Vector",
};

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "Unknown";
  return new Date(dateStr).toLocaleDateString("ja-JP", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

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
      <div className="mx-auto max-w-3xl px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
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
          <div className="flex flex-col gap-3">
            {data.items.map((item) => (
              <Card key={item.newsId} className="border-border">
                <CardHeader className="p-4 pb-2">
                  <div className="flex items-start justify-between gap-2">
                    <CardTitle className="text-sm font-medium leading-snug">
                      <Link
                        href={`/news/${item.newsId}`}
                        className="hover:underline"
                      >
                        {item.originalTitle}
                      </Link>
                    </CardTitle>
                    <WatchlistButton newsId={item.newsId} isWatched={true} />
                  </div>
                </CardHeader>
                <CardContent className="px-4 pb-4 pt-0">
                  <p className="text-[11px] text-muted-foreground">
                    {item.source.name} &middot; {formatDate(item.publishedAt)}
                    {" &middot; Saved "}
                    {formatDate(item.createdAt)}
                  </p>
                </CardContent>
              </Card>
            ))}
          </div>
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
