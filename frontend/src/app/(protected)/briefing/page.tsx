import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";
import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";
import {
  BriefingEmptyRow,
  BriefingRow,
  getBriefingListViewModel,
} from "@/features/briefing";
import { requireSession } from "@/lib/auth/guards";
import { formatDate } from "@/lib/date";

export const metadata: Metadata = {
  title: "Briefing | Vector",
};

async function BriefingListContent() {
  // DAL gate: layout の認可は PPR の別 prerender 単位を守らないため、データ
  // 取得の前にここで認可して static shell 漏洩を塞ぐ。
  await requireSession();
  // build-time prerender を opt out して runtime fill に倒す。`'use cache'`
  // ('hours') と on-demand revalidate の hybrid 戦略は runtime cache 前提。
  await connection();
  const data = await getBriefingListViewModel();

  return (
    <>
      <p className="text-xs text-muted-foreground">
        今週: {formatDate(data.currentWeekStart)} 週
      </p>

      <ul className="flex flex-col divide-y divide-border/60">
        {data.items.map((item) =>
          item.latest === null ? (
            <BriefingEmptyRow key={item.category.id} category={item.category} />
          ) : (
            <BriefingRow
              key={item.category.id}
              category={item.category}
              latest={item.latest}
              isCurrentWeek={item.latest.weekStart === data.currentWeekStart}
            />
          ),
        )}
      </ul>
    </>
  );
}

function BriefingListSkeleton() {
  return (
    <div className="flex flex-col divide-y divide-border/60" aria-hidden="true">
      {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((i) => (
        <div key={i} className="flex flex-col gap-2 py-4">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="h-4 w-full" />
        </div>
      ))}
    </div>
  );
}

export default function BriefingListPage() {
  return (
    <PageContainer maxWidth="3xl" gap={8}>
      <header className="flex flex-col gap-1">
        <h1 className="text-base font-medium">Briefing</h1>
        <p className="text-xs text-muted-foreground">
          AI が公開ニュースから集約した週次解説
        </p>
      </header>
      <Suspense fallback={<BriefingListSkeleton />}>
        <BriefingListContent />
      </Suspense>
    </PageContainer>
  );
}
