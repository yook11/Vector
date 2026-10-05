import type { Metadata } from "next";
import { Suspense } from "react";
import { EmptyState } from "@/components/feedback/EmptyState";
import {
  PageNavigationContent,
  PendingAwareLink,
} from "@/components/layout/PageNavigation";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";
import { ArticleListSkeleton, LoadMoreArticleList } from "@/features/news";
import { loadMoreWatchlist } from "@/features/watchlist";
import { getWatchlist } from "@/features/watchlist/server";
import { requireSession } from "@/lib/auth/guards";

export const metadata: Metadata = {
  title: "Watchlist | Vector",
};

async function WatchlistContent() {
  // DAL gate (多重防御): getWatchlist は authed client で既に fail-closed だが、
  // 401 を踏む前に login へ誘導し、将来 'use cache' 化された際の漏洩も防ぐ。
  await requireSession();
  const data = await getWatchlist();

  return (
    <LoadMoreArticleList
      initialList={data}
      initialWatchedIds={new Set(data.items.map((article) => article.id))}
      loadMore={loadMoreWatchlist}
      showsOnlyWatched
      emptyState={
        <EmptyState
          title="ウォッチした記事がありません"
          description={
            <>
              <PendingAwareLink href="/" className="underline">
                ダッシュボード
              </PendingAwareLink>{" "}
              で記事をブックマークすると、ここに表示されます。
            </>
          }
        />
      }
    />
  );
}

function WatchlistSkeleton() {
  return <ArticleListSkeleton label="ウォッチリストを読み込み中…" />;
}

export default async function WatchlistPage() {
  await requireSession();

  return (
    <PaperSurface>
      <ShellMasthead />
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
        <PageNavigationContent>
          <main className="relative z-10 mx-auto max-w-[1180px] px-[clamp(18px,4vw,40px)] pt-[30px] pb-[80px]">
            <header className="mb-7 flex flex-wrap items-end justify-between gap-4 border-b-[3px] border-double border-[var(--vector-ink)] pb-4">
              <div>
                <p
                  className="text-[14px] font-semibold uppercase tracking-[0.3em] text-[var(--vector-accent-ink)]"
                  style={{ fontFamily: "var(--font-vector-display)" }}
                >
                  WATCHLIST
                </p>
                <h1
                  className="mt-1.5 text-[clamp(28px,3.6vw,40px)] font-extrabold tracking-[0.01em] text-[var(--vector-ink)]"
                  style={{ fontFamily: "var(--font-vector-serif)" }}
                >
                  ウォッチリスト
                </h1>
              </div>
            </header>
            <Suspense fallback={<WatchlistSkeleton />}>
              <WatchlistContent />
            </Suspense>
          </main>
        </PageNavigationContent>
      </div>
    </PaperSurface>
  );
}
