import type { Metadata } from "next";
import { getWeeklyTrends } from "@/features/digest/api/get-weekly-trends";
import { HotEntityList } from "@/features/digest/components/HotEntityList";
import { HotTopicList } from "@/features/digest/components/HotTopicList";
import { NewEntityList } from "@/features/digest/components/NewEntityList";
import { formatDate } from "@/lib/date";

export const metadata: Metadata = {
  title: "Weekly Trends | Vector",
};

// snapshot は worker-digest により JST 月曜 00:05 に週次更新されるため、
// 24 時間 ISR で十分。
export const revalidate = 86400;

export default async function WeeklyTrendsPage() {
  const data = await getWeeklyTrends();

  if (!data.weekStart || data.categories.length === 0) {
    return (
      <main className="h-full overflow-y-auto">
        <div className="mx-auto max-w-5xl px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
          <h1 className="text-base font-medium">Weekly Trends</h1>
          <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
            <p className="text-sm font-medium">
              週次トレンドはまだ生成されていません
            </p>
            <p className="text-xs mt-1">
              次回の自動生成は JST 月曜 00:05 に予定されています
            </p>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-10">
        <header className="flex flex-col gap-2">
          <h1 className="text-base font-medium">Weekly Trends</h1>
          <p className="text-xs text-muted-foreground">
            {formatDate(data.weekStart)} – {formatDate(data.weekEnd)}
            {data.sourceAnalysisCount !== null && (
              <span className="ml-2">
                · {data.sourceAnalysisCount} 件の分析を集計
              </span>
            )}
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
                  <h3 className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    Hot Entities
                  </h3>
                  <HotEntityList entities={category.trendingEntities} />
                </div>
                <div className="flex flex-col gap-3">
                  <h3 className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    Hot Topics
                  </h3>
                  <HotTopicList topics={category.trendingTopics} />
                </div>
                <div className="flex flex-col gap-3">
                  <h3 className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    New Entities
                  </h3>
                  <NewEntityList entities={category.newEntities} />
                </div>
              </div>
            </section>
          ))}
        </div>
      </div>
    </main>
  );
}
