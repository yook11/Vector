import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getTrendsViewModel,
  TrendsEmptyState,
  TrendsView,
} from "@/features/trends";
import { requireSession } from "@/lib/auth/guards";

export const metadata: Metadata = {
  title: "トレンド | Vector",
};

async function TrendsContent() {
  // DAL gate: layout の認可は PPR の別 prerender 単位を守らないため、データ
  // 取得の前にここで認可して static shell 漏洩を塞ぐ。
  await requireSession();
  // build-time prerender を opt out して runtime fill に倒す。
  await connection();
  const data = await getTrendsViewModel();

  if (data.state === "empty") {
    return <TrendsEmptyState />;
  }
  return <TrendsView data={data} />;
}

function TrendsContentSkeleton() {
  return (
    <>
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="relative z-10 mx-auto max-w-[1100px] px-5 pt-8 text-sm font-medium text-[var(--vector-ink-soft)] sm:px-8 lg:px-10"
      >
        トレンドを読み込み中…
      </p>
      <div
        className="relative z-10 mx-auto max-w-[1100px] px-5 pt-3 pb-8 sm:px-8 lg:px-10 motion-reduce:animate-none motion-reduce:[&_[data-slot=skeleton]]:animate-none"
        aria-hidden="true"
      >
        {/* マストヘッドスケルトン (TrendsMasthead の eyebrow/H1/サブ/メタ行/罫線に対応) */}
        <div className="mb-8">
          <Skeleton className="h-3 w-28 mb-3" />
          <Skeleton className="h-10 w-72 mb-3 sm:w-[420px]" />
          <Skeleton className="h-4 w-56 mb-3" />
          <Skeleton className="h-3 w-80 mb-5" />
          <div className="h-[3px] bg-[var(--vector-line)] rounded-sm" />
        </div>

        {/* カテゴリ x 3 のスケルトン */}
        <div className="flex flex-col gap-12">
          {[0, 1, 2].map((section) => (
            <section key={section} className="flex flex-col gap-5">
              <div className="flex items-center gap-3">
                <Skeleton className="size-4 rounded-[2px]" />
                <Skeleton className="h-6 w-28" />
                <Skeleton className="h-3 w-16" />
                <div className="flex-1 h-px bg-[var(--vector-line)]" />
              </div>
              <div className="grid gap-8 md:grid-cols-2">
                {[0, 1].map((col) => (
                  <div key={col} className="flex flex-col gap-3">
                    <div className="pb-2 border-b-2 border-[var(--vector-line)] flex gap-2 items-baseline">
                      <Skeleton className="h-3 w-24" />
                      <Skeleton className="h-4 w-16" />
                    </div>
                    {[0, 1, 2, 3, 4].map((row) => (
                      <Skeleton key={row} className="h-10 w-full rounded-md" />
                    ))}
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </>
  );
}

export default function TrendsPage() {
  return (
    <Suspense fallback={<TrendsContentSkeleton />}>
      <TrendsContent />
    </Suspense>
  );
}
