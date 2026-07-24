import { Suspense } from "react";
import { getProtectedNavItems } from "@/components/layout/nav-items";
import { PageNavigationContent } from "@/components/layout/PageNavigation";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import {
  formatPaperMastheadDate,
  PaperSurface,
  PaperTexture,
} from "@/components/paper";
import { UserMenu } from "@/features/auth";
import {
  DashboardArticleListSkeleton,
  DashboardMasthead,
  DashboardPaperArticleList,
  getArticles,
  getCategories,
  getLatestArticleDate,
  PaperNewsControls,
  PaperNewsPagination,
  PaperNewsResultSummary,
  parseArticleQuery,
} from "@/features/news";
import { getWatchlistIds } from "@/features/watchlist";
import { requireSession } from "@/lib/auth/guards";
import { narrowRole } from "@/lib/auth/role";
import type { SearchParams } from "@/lib/types/route";
import type { ArticleQuery } from "@/types";

interface DashboardPageProps {
  searchParams: Promise<SearchParams>;
}

export default async function DashboardPage({
  searchParams,
}: DashboardPageProps) {
  const raw = await searchParams;
  const { query: filters } = parseArticleQuery(raw);
  // gate はデータ取得の前に直列で置く。未認証時に cached fetch が走るのを防ぐ。
  const session = await requireSession();
  const isAdmin = narrowRole(session.user.role) === "admin";
  const navItems = getProtectedNavItems(isAdmin);

  // 独立した request は最初にまとめて開始し、カテゴリ待ちで外枠を止めない。
  const categoriesPromise = getCategories();
  const articlesPromise = getArticles(filters);
  const watchedIdsPromise = getWatchlistIds();

  return (
    <PaperSurface>
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
        <Suspense fallback={<DashboardInitialSkeleton />}>
          <DashboardContent
            articlesPromise={articlesPromise}
            categoriesPromise={categoriesPromise}
            filters={filters}
            navItems={navItems}
            watchedIdsPromise={watchedIdsPromise}
          />
        </Suspense>
      </div>
    </PaperSurface>
  );
}

function DashboardInitialSkeleton() {
  const bar =
    "animate-pulse motion-reduce:animate-none rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_10%,transparent)]";

  return (
    <>
      <div aria-hidden="true" className="relative z-10 px-5 sm:px-8 lg:px-10">
        <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-4 pt-5 pb-3">
          <div className={`h-4 w-24 ${bar}`} />
          <div className={`h-5 w-56 ${bar}`} />
          <div className={`h-4 w-32 justify-self-end ${bar}`} />
        </div>
        <div className="flex items-center justify-center gap-4 py-5 sm:gap-6">
          <div className={`h-px flex-1 ${bar}`} />
          <div className={`h-14 w-48 ${bar}`} />
          <div className={`h-px flex-1 ${bar}`} />
        </div>
        <div className="mb-3 border-t-[3px] border-double border-[var(--vector-ink)]" />
        <div className="flex gap-2 overflow-hidden pb-5">
          {[0, 1, 2, 3].map((item) => (
            <div key={item} className={`h-9 w-20 shrink-0 ${bar}`} />
          ))}
        </div>
      </div>
      <section
        aria-hidden="true"
        className="relative z-10 mx-5 mb-7 flex items-center justify-between border-b border-[var(--vector-ink)] pb-3.5 sm:mx-8 lg:mx-10"
      >
        <div className={`h-5 w-36 ${bar}`} />
        <div className={`h-9 w-28 ${bar}`} />
      </section>
      <main className="relative z-10 px-5 pb-14 sm:px-8 lg:px-10">
        <DashboardArticleListSkeleton label="記事を更新中…" />
      </main>
    </>
  );
}

async function DashboardContent({
  articlesPromise,
  categoriesPromise,
  filters,
  navItems,
  watchedIdsPromise,
}: {
  articlesPromise: ReturnType<typeof getArticles>;
  categoriesPromise: ReturnType<typeof getCategories>;
  filters: ArticleQuery;
  navItems: ReturnType<typeof getProtectedNavItems>;
  watchedIdsPromise: ReturnType<typeof getWatchlistIds>;
}) {
  const categoriesData = await categoriesPromise;

  // フィルタ変更のたびに key で再マウントして、記事領域だけで再取得を伝える。
  const sectionKey = `${filters.category ?? "all"}|${filters.sortOrder ?? "desc"}|${filters.perPage ?? ""}|${filters.page ?? 1}`;
  const categoryProps =
    filters.category !== undefined ? { activeCategory: filters.category } : {};

  return (
    <>
      <DashboardMasthead
        categories={categoriesData.items}
        currentQuery={filters}
        dateSlot={
          <Suspense
            fallback={
              <span
                aria-hidden="true"
                className="hidden h-4 w-32 rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_10%,transparent)] sm:inline-block"
              />
            }
          >
            <MastheadDate articlesPromise={articlesPromise} />
          </Suspense>
        }
        navItems={navItems}
        themeSlot={<ThemeToggle />}
        userMenuSlot={
          <UserMenu
            compact
            buttonClassName="rounded-none text-[var(--vector-ink-muted)] hover:bg-transparent hover:text-[var(--vector-accent)]"
            emailClassName="text-[var(--vector-ink-muted)]"
          />
        }
        {...categoryProps}
      />

      <PageNavigationContent>
        <section className="relative z-10 mx-5 mb-7 flex flex-wrap items-center justify-between gap-3 border-b border-[var(--vector-ink)] pb-3.5 sm:mx-8 lg:mx-10">
          <Suspense
            key={sectionKey}
            fallback={
              <span
                aria-hidden="true"
                className="h-5 w-36 rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_10%,transparent)]"
              />
            }
          >
            <PaperNewsResultSummary
              filters={filters}
              categories={categoriesData.items}
              {...categoryProps}
            />
          </Suspense>
          <PaperNewsControls />
        </section>

        <main className="relative z-10 px-5 pb-14 sm:px-8 lg:px-10">
          <Suspense
            key={sectionKey}
            fallback={<DashboardArticleListSkeleton label="記事を更新中…" />}
          >
            <DashboardArticleSection
              articlesPromise={articlesPromise}
              watchedIdsPromise={watchedIdsPromise}
            />
          </Suspense>
        </main>
      </PageNavigationContent>
    </>
  );
}

async function MastheadDate({
  articlesPromise,
}: {
  articlesPromise: ReturnType<typeof getArticles>;
}) {
  const data = await articlesPromise;
  return (
    <span
      className="hidden shrink-0 text-[12.5px] italic tracking-[0.04em] text-[var(--vector-ink-muted)] sm:inline"
      style={{ fontFamily: "var(--font-vector-display)" }}
    >
      {formatPaperMastheadDate(getLatestArticleDate(data.items))}
    </span>
  );
}

async function DashboardArticleSection({
  articlesPromise,
  watchedIdsPromise,
}: {
  articlesPromise: ReturnType<typeof getArticles>;
  watchedIdsPromise: ReturnType<typeof getWatchlistIds>;
}) {
  const [newsData, watchedIds] = await Promise.all([
    articlesPromise,
    watchedIdsPromise,
  ]);
  return (
    <>
      <DashboardPaperArticleList
        items={newsData.items}
        watchedIds={watchedIds}
      />
      <PaperNewsPagination
        page={newsData.page}
        totalPages={newsData.totalPages}
      />
    </>
  );
}
