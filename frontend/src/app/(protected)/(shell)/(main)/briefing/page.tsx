import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import {
  BriefingIndexView,
  getBriefingListViewModel,
} from "@/features/briefing";
import { requireSession } from "@/lib/auth/guards";

export const metadata: Metadata = { title: "Briefing | Vector" };

async function BriefingListContent() {
  // DAL gate: layout の認可は PPR の別 prerender 単位を守らないため、データ
  // 取得の前にここで認可する。
  await requireSession();
  // build-time prerender を opt out して runtime fill に倒す。
  await connection();
  const data = await getBriefingListViewModel();
  return <BriefingIndexView data={data} />;
}

function BriefingListSkeleton() {
  return (
    <>
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="relative z-10 mx-auto max-w-[1180px] px-[clamp(18px,4vw,40px)] pt-[30px] text-sm font-medium text-[var(--vector-ink-soft)]"
      >
        Briefingを読み込み中…
      </p>
      <div
        className="relative z-10 mx-auto max-w-[1180px] px-[clamp(18px,4vw,40px)] pt-3 pb-[80px] motion-reduce:animate-none motion-reduce:[&_[data-slot=skeleton]]:animate-none"
        aria-hidden="true"
      >
        {/* マストヘッドスケルトン */}
        <div className="mb-[18px] flex flex-wrap items-center justify-between gap-4 border-b-[3px] border-double border-[var(--vector-ink)] pb-4">
          <Skeleton className="h-3.5 w-40" />
          <Skeleton className="h-3 w-56" />
        </div>
        <div className="flex items-baseline gap-4">
          <Skeleton className="h-9 w-72 sm:w-[420px]" />
          <Skeleton className="h-4 w-28" />
        </div>
        <Skeleton className="mt-[10px] h-3 w-80" />

        {/* バンドカード x 5 のスケルトン (BriefingIndexView の mt-2 に合わせ CLS を抑える) */}
        <div className="mt-2 flex flex-col gap-[16px]">
          {[0, 1, 2, 3, 4].map((card) => (
            <div
              key={card}
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
    </>
  );
}

export default function BriefingListPage() {
  return (
    <Suspense fallback={<BriefingListSkeleton />}>
      <BriefingListContent />
    </Suspense>
  );
}
