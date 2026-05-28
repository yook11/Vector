import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { EmptyState } from "@/components/feedback/EmptyState";
import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DEFAULT_PER_PAGE,
  isPerPageOption,
  NewsList,
  NewsPagination,
  type PerPageOption,
  PerPageSelect,
  parseArticleQuery,
} from "@/features/news";
import { getWatchlist } from "@/features/watchlist";
import type { SearchParams } from "@/lib/types/route";

export const metadata: Metadata = {
  title: "Watchlist | Vector",
};

interface WatchlistPageProps {
  searchParams: Promise<SearchParams>;
}

async function WatchlistContent({
  page,
  perPage,
}: {
  page: number;
  perPage?: number;
}) {
  const data = await getWatchlist(page, perPage);

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
  const perPage = query.perPage;
  // PerPageSelect の current は parser で allowlist 通過済の値を文字列化、
  // 未指定なら DEFAULT_PER_PAGE。allowlist 外は parser 段で undefined 化されている。
  const perPageSelectValue: PerPageOption =
    perPage !== undefined && isPerPageOption(String(perPage))
      ? (String(perPage) as PerPageOption)
      : DEFAULT_PER_PAGE;

  return (
    <PageContainer>
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-base font-medium">Watchlist</h1>
        <Suspense fallback={null}>
          <PerPageSelect current={perPageSelectValue} />
        </Suspense>
      </div>
      {/* URL searchParams を JSON 化して Suspense key に与えることで、
          searchParams が変化したときに fallback (skeleton) を再表示する。
          dashboard 側 (`(protected)/page.tsx`) と統一した戦略。今後
          searchParams が増えた際に key 候補の追加漏れを防ぐ。 */}
      <Suspense
        key={JSON.stringify({ page, perPage })}
        fallback={<WatchlistSkeleton />}
      >
        <WatchlistContent page={page} perPage={perPage} />
      </Suspense>
    </PageContainer>
  );
}
