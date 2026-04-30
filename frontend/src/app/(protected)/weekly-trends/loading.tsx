import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";

export default function WeeklyTrendsLoading() {
  return (
    <PageContainer gap={10}>
      <header className="flex flex-col gap-2">
        <Skeleton className="h-5 w-32" />
        <Skeleton className="h-3 w-60" />
      </header>

      {Array.from({ length: 3 }).map((_, sectionIdx) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton sections
        <section key={sectionIdx} className="flex flex-col gap-5">
          <Skeleton className="h-4 w-32" />
          <div className="grid gap-8 md:grid-cols-3">
            {Array.from({ length: 3 }).map((_, colIdx) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton columns
              <div key={colIdx} className="flex flex-col gap-3">
                <Skeleton className="h-3 w-20" />
                {Array.from({ length: 5 }).map((_, rowIdx) => (
                  <Skeleton
                    // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton rows
                    key={rowIdx}
                    className="h-9 w-full rounded-md"
                  />
                ))}
              </div>
            ))}
          </div>
        </section>
      ))}
    </PageContainer>
  );
}
