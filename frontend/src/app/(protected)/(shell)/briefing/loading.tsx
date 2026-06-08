import { Skeleton } from "@/components/ui/skeleton";

export default function BriefingLoading() {
  return (
    <div className="min-h-dvh bg-[var(--vector-paper,#f7f3ec)] [--vector-ink:#221c16] [--vector-line:#e4dccc] [--vector-paper:#f7f3ec]">
      <div className="mx-auto max-w-[1180px] px-[clamp(18px,4vw,40px)] pt-[30px] pb-[80px]">
        {/* マストヘッドスケルトン */}
        <div
          className="mb-[18px] flex flex-wrap items-center justify-between gap-4 border-b-[3px] border-double border-[var(--vector-ink)] pb-4"
          aria-hidden="true"
        >
          <Skeleton className="h-3.5 w-40" />
          <Skeleton className="h-3 w-56" />
        </div>
        <div className="flex items-baseline gap-4" aria-hidden="true">
          <Skeleton className="h-9 w-72 sm:w-[420px]" />
          <Skeleton className="h-4 w-28" />
        </div>
        <Skeleton className="mt-[10px] h-3 w-80" />

        {/* バンドカード x 5 のスケルトン */}
        <div className="mt-6 flex flex-col gap-[16px]" aria-hidden="true">
          {[0, 1, 2, 3, 4].map((i) => (
            <div
              key={i}
              className="overflow-hidden rounded-[3px_3px_12px_12px] border border-[var(--vector-line)]"
            >
              <div className="flex items-center gap-3 border-b border-[var(--vector-line)] px-[clamp(20px,2.4vw,28px)] py-[11px]">
                <Skeleton className="size-[11px]" />
                <Skeleton className="h-3 w-16" />
                <Skeleton className="h-3.5 w-20" />
                <Skeleton className="ml-auto h-4 w-10" />
              </div>
              <div className="flex flex-col gap-[10px] px-[clamp(20px,2.4vw,28px)] pt-[clamp(17px,1.9vw,22px)] pb-[clamp(16px,1.8vw,20px)]">
                <Skeleton className="h-6 w-3/4" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-5/6" />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
