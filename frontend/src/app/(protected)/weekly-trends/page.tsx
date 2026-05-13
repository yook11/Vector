import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";
import { EmptyState } from "@/components/feedback/EmptyState";
import { SectionLabel } from "@/components/feedback/SectionLabel";
import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getWeeklyTrendsViewModel,
  HotEntityList,
  NewEntityList,
} from "@/features/digest";
import { formatDate } from "@/lib/date";

export const metadata: Metadata = {
  title: "Weekly Trends | Vector",
};

async function WeeklyTrendsContent() {
  // build-time prerender を opt out して runtime fill に倒す。`'use cache'`
  // ('days') は runtime cache を共有するため backend hit は週 1 回程度に
  // 留まるが、build phase では backend 不要にすることで Fly.io 等の段階的
  // deploy (frontend → backend) と整合させる。
  await connection();
  const data = await getWeeklyTrendsViewModel();

  if (data.state === "empty") {
    return (
      <EmptyState
        title="週次トレンドはまだ生成されていません"
        description="次回の自動生成は JST 毎日 00:05 に予定されています"
      />
    );
  }

  return (
    <>
      <p className="text-xs text-muted-foreground">
        {formatDate(data.windowStart)} – {formatDate(data.windowEnd)}
        <span className="ml-2">
          · {data.sourceAnalysisCount} 件の分析を集計
        </span>
      </p>

      <div className="flex flex-col gap-12">
        {data.categories.map((category) => (
          <section key={category.categoryId} className="flex flex-col gap-5">
            <h2 className="text-sm font-medium tracking-tight">
              {category.categoryName}
            </h2>
            <div className="grid gap-8 md:grid-cols-2">
              <div className="flex flex-col gap-3">
                <SectionLabel as="h3">Hot Entities</SectionLabel>
                <HotEntityList entities={category.trendingEntities} />
              </div>
              <div className="flex flex-col gap-3">
                <SectionLabel as="h3">New Entities</SectionLabel>
                <NewEntityList entities={category.newEntities} />
              </div>
            </div>
          </section>
        ))}
      </div>
    </>
  );
}

function WeeklyTrendsSkeleton() {
  return (
    <div className="flex flex-col gap-12" aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <section key={i} className="flex flex-col gap-5">
          <Skeleton className="h-5 w-32" />
          <div className="grid gap-8 md:grid-cols-2">
            {[0, 1].map((j) => (
              <div key={j} className="flex flex-col gap-3">
                <Skeleton className="h-3 w-20" />
                {[0, 1, 2, 3].map((k) => (
                  <Skeleton key={k} className="h-4 w-full" />
                ))}
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

export default function WeeklyTrendsPage() {
  return (
    <PageContainer gap={10}>
      <h1 className="text-base font-medium">Weekly Trends</h1>
      <Suspense fallback={<WeeklyTrendsSkeleton />}>
        <WeeklyTrendsContent />
      </Suspense>
    </PageContainer>
  );
}
