import { getProtectedNavItems } from "@/components/layout/nav-items";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { UserMenu } from "@/features/auth";
import {
  DashboardMasthead,
  DashboardPaperArticleList,
  formatPaperMastheadDate,
  getArticles,
  getCategories,
  getLatestArticleDate,
  PaperNewsControls,
  PaperNewsPagination,
  PaperTexture,
  parseArticleQuery,
} from "@/features/news";
import { getWatchlistIds } from "@/features/watchlist";
import { requireSession } from "@/lib/auth/guards";
import { narrowRole } from "@/lib/auth/role";
import type { SearchParams } from "@/lib/types/route";

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
  const [newsData, watchedIds, categoriesData] = await Promise.all([
    getArticles(filters),
    getWatchlistIds(),
    getCategories(),
  ]);
  const displayDate = formatPaperMastheadDate(
    getLatestArticleDate(newsData.items),
  );

  // EOP 下で undefined を optional prop に明示代入できないため、
  // 条件付き spread で「未指定 or 値あり」を表現する。
  const categoryProps =
    filters.category !== undefined ? { activeCategory: filters.category } : {};

  return (
    <div
      className="min-h-dvh bg-[var(--vector-paper)] text-[var(--vector-ink)] [--vector-accent:#0fa89c] [--vector-accent-ink:#08756f] [--vector-ink:#221c16] [--vector-ink-muted:#938a7c] [--vector-ink-soft:#5c544a] [--vector-line:#e4dccc] [--vector-paper:#f7f3ec] [--vector-rule:#d5ccbc] dark:[--vector-accent:#2dd4bf] dark:[--vector-accent-ink:#67e8d8] dark:[--vector-ink:#f3eee4] dark:[--vector-ink-muted:#8a8173] dark:[--vector-ink-soft:#b7ae9f] dark:[--vector-line:#332c23] dark:[--vector-paper:#14110b] dark:[--vector-rule:#40382d]"
      style={{ fontFamily: "var(--font-vector-sans)" }}
    >
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
        <DashboardMasthead
          categories={categoriesData.items}
          currentQuery={filters}
          displayDate={displayDate}
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

        <section className="relative z-10 mx-5 mb-7 flex flex-wrap items-center justify-end gap-3 border-b border-[var(--vector-ink)] pb-3.5 sm:mx-8 lg:mx-10">
          <PaperNewsControls />
        </section>

        <main className="relative z-10 px-5 pb-14 sm:px-8 lg:px-10">
          <DashboardPaperArticleList
            items={newsData.items}
            watchedIds={watchedIds}
          />
          <PaperNewsPagination
            page={newsData.page}
            totalPages={newsData.totalPages}
          />
        </main>
      </div>
    </div>
  );
}
