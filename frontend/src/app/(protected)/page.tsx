import { Suspense } from "react";
import { getProtectedNavItems } from "@/components/layout/nav-items";
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
  const categoriesData = await getCategories();

  // 記事取得は子の Suspense へ閉じ込め、フィルタ変更のたびに key で再マウントして
  // skeleton を出す。これにより切り替え中であることがコンテンツ領域で伝わる。
  const sectionKey = `${filters.category ?? "all"}|${filters.sortOrder ?? "desc"}|${filters.perPage ?? ""}|${filters.page ?? 1}`;

  // EOP 下で undefined を optional prop に明示代入できないため、
  // 条件付き spread で「未指定 or 値あり」を表現する。
  const categoryProps =
    filters.category !== undefined ? { activeCategory: filters.category } : {};

  return (
    <PaperSurface>
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
        <DashboardMasthead
          categories={categoriesData.items}
          currentQuery={filters}
          dateSlot={
            <Suspense fallback={null}>
              <MastheadDate filters={filters} />
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

        <section className="relative z-10 mx-5 mb-7 flex flex-wrap items-center justify-between gap-3 border-b border-[var(--vector-ink)] pb-3.5 sm:mx-8 lg:mx-10">
          <Suspense key={sectionKey} fallback={<span className="h-5" />}>
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
            fallback={<DashboardArticleListSkeleton />}
          >
            <DashboardArticleSection filters={filters} />
          </Suspense>
        </main>
      </div>
    </PaperSurface>
  );
}

async function MastheadDate({ filters }: { filters: ArticleQuery }) {
  const data = await getArticles(filters);
  return (
    <span
      className="hidden shrink-0 text-[12.5px] italic tracking-[0.04em] text-[var(--vector-ink-muted)] sm:inline"
      style={{ fontFamily: "var(--font-vector-display)" }}
    >
      {formatPaperMastheadDate(getLatestArticleDate(data.items))}
    </span>
  );
}

async function DashboardArticleSection({ filters }: { filters: ArticleQuery }) {
  const [newsData, watchedIds] = await Promise.all([
    getArticles(filters),
    getWatchlistIds(),
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
