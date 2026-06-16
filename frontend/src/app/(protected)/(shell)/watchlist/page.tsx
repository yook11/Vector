import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { EmptyState } from "@/components/feedback/EmptyState";
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
  page,
  perPage,
}: {
  page: number;
  perPage?: number;
}) {
  // DAL gate (多重防御): getWatchlist は authed client で既に fail-closed だが、
  // 401 を踏む前に login へ誘導し、将来 'use cache' 化された際の漏洩も防ぐ。
  await requireSession();
  const data = await getWatchlist(page, perPage);

  if (data.items.length === 0) {
    return (
      <EmptyState
        title="ウォッチした記事がありません"
        description={
          <>
            <Link href="/" className="underline">
              ダッシュボード
            </Link>{" "}
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
    <PaperSurface>
      <ShellMasthead />
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
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
            <Suspense fallback={null}>
              <PerPageSelect current={perPageSelectValue} />
            </Suspense>
          </header>
          {/* URL searchParams を JSON 化して Suspense key に与えることで、
              searchParams が変化したときに fallback (skeleton) を再表示する。
              dashboard 側 (`(protected)/page.tsx`) と統一した戦略。今後
              searchParams が増えた際に key 候補の追加漏れを防ぐ。 */}
          <Suspense
            key={JSON.stringify({ page, perPage })}
            fallback={<DashboardArticleListSkeleton />}
          >
            <WatchlistContent page={page} perPage={perPage} />
          </Suspense>
        </main>
      </div>
    </PaperSurface>
  );
}
