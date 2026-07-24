import type { Metadata } from "next";
import { Suspense } from "react";
import { EmptyState } from "@/components/feedback/EmptyState";
import {
  PageNavigationContent,
  PendingAwareLink,
} from "@/components/layout/PageNavigation";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";
import {
  DashboardArticleListSkeleton,
  DashboardPaperArticleList,
  DEFAULT_PER_PAGE,
  isPerPageOption,
  PaperNewsPagination,
  type PerPageOption,
  PerPageSelect,
  parseArticleQuery,
} from "@/features/news";
import { getWatchlist } from "@/features/watchlist";
import { requireSession } from "@/lib/auth/guards";
import type { SearchParams } from "@/lib/types/route";

export const metadata: Metadata = {
  title: "Watchlist | Vector",
};

interface WatchlistPageProps {
  searchParams: Promise<SearchParams>;
}

async function WatchlistContent({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  // DAL gate (多重防御): getWatchlist は authed client で既に fail-closed だが、
  // 401 を踏む前に login へ誘導し、将来 'use cache' 化された際の漏洩も防ぐ。
  await requireSession();
  const raw = await searchParams;
  const { query } = parseArticleQuery(raw);
  const page = query.page ?? 1;
  const perPage = query.perPage;
  const data = await getWatchlist(page, perPage);

  if (data.items.length === 0) {
    return (
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
    );
  }

  // /watchlist 配下の記事は全件 watched が定義上自明なので、追加 fetch を
  // せず item ID から直接 Set を作る。
  const watchedIds = new Set(data.items.map((a) => a.id));

  return (
    <>
      <DashboardPaperArticleList items={data.items} watchedIds={watchedIds} />
      <PaperNewsPagination page={data.page} totalPages={data.totalPages} />
    </>
  );
}

async function PerPageControl({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const raw = await searchParams;
  const { query } = parseArticleQuery(raw);
  const perPage = query.perPage;
  const value: PerPageOption =
    perPage !== undefined && isPerPageOption(String(perPage))
      ? (String(perPage) as PerPageOption)
      : DEFAULT_PER_PAGE;
  return <PerPageSelect current={value} />;
}

function PerPageControlPlaceholder() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-9 w-28 rounded-md bg-[color-mix(in_oklab,var(--vector-ink)_10%,transparent)]"
    />
  );
}

function WatchlistSkeleton() {
  return <DashboardArticleListSkeleton label="ウォッチリストを読み込み中…" />;
}

export default async function WatchlistPage({
  searchParams,
}: WatchlistPageProps) {
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
              <Suspense fallback={<PerPageControlPlaceholder />}>
                <PerPageControl searchParams={searchParams} />
              </Suspense>
            </header>
            <Suspense fallback={<WatchlistSkeleton />}>
              <WatchlistContent searchParams={searchParams} />
            </Suspense>
          </main>
        </PageNavigationContent>
      </div>
    </PaperSurface>
  );
}
