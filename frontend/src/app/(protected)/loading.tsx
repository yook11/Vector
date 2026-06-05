import {
  DashboardArticleListSkeleton,
  PaperSurface,
  PaperTexture,
} from "@/features/news";

export default function DashboardLoading() {
  return (
    <PaperSurface>
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
        <header className="relative z-10 px-5 sm:px-8 lg:px-10">
          <div className="flex items-center justify-center gap-4 py-9 sm:gap-6">
            <span className="h-px flex-1 bg-[color-mix(in_oklab,var(--vector-ink)_18%,transparent)]" />
            <div className="h-12 w-48 animate-pulse rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_10%,transparent)] sm:h-16 sm:w-64" />
            <span className="h-px flex-1 bg-[color-mix(in_oklab,var(--vector-ink)_18%,transparent)]" />
          </div>
          <div className="mb-7 border-t-[3px] border-double border-[var(--vector-ink)]" />
        </header>
        <main className="relative z-10 px-5 pb-14 sm:px-8 lg:px-10">
          <DashboardArticleListSkeleton />
        </main>
      </div>
    </PaperSurface>
  );
}
