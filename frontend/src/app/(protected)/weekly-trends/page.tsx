import type { Metadata } from "next";
import { EmptyState } from "@/components/feedback/EmptyState";
import { SectionLabel } from "@/components/feedback/SectionLabel";
import { PageContainer } from "@/components/layout/PageContainer";
import {
  getWeeklyTrends,
  HotEntityList,
  HotTopicList,
  NewEntityList,
} from "@/features/digest";
import { formatDate } from "@/lib/date";

export const metadata: Metadata = {
  title: "Weekly Trends | Vector",
};

export default async function WeeklyTrendsPage() {
  const data = await getWeeklyTrends();

  if (data.state === "empty") {
    return (
      <PageContainer>
        <h1 className="text-base font-medium">Weekly Trends</h1>
        <EmptyState
          title="週次トレンドはまだ生成されていません"
          description="次回の自動生成は JST 月曜 00:05 に予定されています"
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer gap={10}>
      <header className="flex flex-col gap-2">
        <h1 className="text-base font-medium">Weekly Trends</h1>
        <p className="text-xs text-muted-foreground">
          {formatDate(data.weekStart)} – {formatDate(data.weekEnd)}
          <span className="ml-2">
            · {data.sourceAnalysisCount} 件の分析を集計
          </span>
        </p>
      </header>

      <div className="flex flex-col gap-12">
        {data.categories.map((category) => (
          <section key={category.categoryId} className="flex flex-col gap-5">
            <h2 className="text-sm font-medium tracking-tight">
              {category.categoryName}
            </h2>
            <div className="grid gap-8 md:grid-cols-3">
              <div className="flex flex-col gap-3">
                <SectionLabel as="h3">Hot Entities</SectionLabel>
                <HotEntityList entities={category.trendingEntities} />
              </div>
              <div className="flex flex-col gap-3">
                <SectionLabel as="h3">Hot Topics</SectionLabel>
                <HotTopicList topics={category.trendingTopics} />
              </div>
              <div className="flex flex-col gap-3">
                <SectionLabel as="h3">New Entities</SectionLabel>
                <NewEntityList entities={category.newEntities} />
              </div>
            </div>
          </section>
        ))}
      </div>
    </PageContainer>
  );
}
