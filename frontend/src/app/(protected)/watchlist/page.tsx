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
    <main className="mx-auto max-w-3xl p-6 space-y-6">
      <h1 className="text-2xl font-bold">Watchlist</h1>

      {data.items.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <p className="text-lg font-medium">No saved articles</p>
          <p className="text-sm">
            Bookmark articles from the{" "}
            <Link href="/" className="underline">
              dashboard
            </Link>{" "}
            to see them here.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {data.items.map((item) => (
            <Card key={item.id}>
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between gap-2">
                  <CardTitle className="text-base leading-snug">
                    <Link
                      href={`/news/${item.newsArticleId}`}
                      className="hover:underline"
                    >
                      {item.titleOriginal}
                    </Link>
                  </CardTitle>
                  <WatchlistButton
                    newsArticleId={item.newsArticleId}
                    isWatched={true}
                  />
                </div>
              </CardHeader>
              <CardContent>
                <p className="text-xs text-muted-foreground">
                  {item.source} &middot; {formatDate(item.publishedAt)}
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
              className="text-sm text-muted-foreground hover:text-foreground"
            >
              Previous
            </Link>
          )}
          <span className="text-sm text-muted-foreground">
            Page {data.page} of {data.totalPages}
          </span>
          {page < data.totalPages && (
            <Link
              href={`/watchlist?page=${page + 1}`}
              className="text-sm text-muted-foreground hover:text-foreground"
            >
              Next
            </Link>
          )}
        </div>
      )}
    </main>
  );
}
