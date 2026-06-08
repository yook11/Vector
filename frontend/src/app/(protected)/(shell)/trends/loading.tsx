import { Skeleton } from "@/components/ui/skeleton";

export default function TrendsLoading() {
  return (
    <div className="min-h-dvh bg-[var(--vector-paper,#f7f3ec)] [--vector-paper:#f7f3ec] [--vector-ink:#221c16] [--vector-line:#e4dccc]">
      <div className="px-5 py-8 sm:px-8 lg:px-10 max-w-[1100px] mx-auto">
        {/* マストヘッドスケルトン */}
        <div className="mb-8" aria-hidden="true">
          <Skeleton className="h-3 w-28 mb-3" />
          <Skeleton className="h-10 w-72 mb-3 sm:w-[420px]" />
          <Skeleton className="h-4 w-56 mb-3" />
          <Skeleton className="h-3 w-80 mb-5" />
          <div className="h-[3px] bg-[var(--vector-line)] rounded-sm" />
        </div>

        {/* カテゴリ x 3 のスケルトン */}
        <div className="flex flex-col gap-12" aria-hidden="true">
          {[0, 1, 2].map((i) => (
            <section key={i} className="flex flex-col gap-5">
              {/* 見出し行 */}
              <div className="flex items-center gap-3">
                <Skeleton className="size-4 rounded-[2px]" />
                <Skeleton className="h-6 w-28" />
                <Skeleton className="h-3 w-16" />
                <div className="flex-1 h-px bg-[var(--vector-line)]" />
              </div>
              {/* 2カラム */}
              <div className="grid gap-8 md:grid-cols-2">
                {[0, 1].map((j) => (
                  <div key={j} className="flex flex-col gap-3">
                    {/* ColumnHead */}
                    <div className="pb-2 border-b-2 border-[var(--vector-line)] flex gap-2 items-baseline">
                      <Skeleton className="h-3 w-24" />
                      <Skeleton className="h-4 w-16" />
                    </div>
                    {/* 行 x 5 */}
                    {[0, 1, 2, 3, 4].map((k) => (
                      <Skeleton key={k} className="h-10 w-full rounded-md" />
                    ))}
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
