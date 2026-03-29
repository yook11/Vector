import { Skeleton } from "@/components/ui/skeleton";

export default function DashboardLoading() {
  return (
    <div className="flex h-full gap-0">
      <aside className="hidden lg:flex w-64 shrink-0 flex-col border-r border-border p-6 gap-4">
        <Skeleton className="h-5 w-24 mb-1" />
        {Array.from({ length: 6 }).map((_, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
          <Skeleton key={i} className="h-9 w-full rounded-xl" />
        ))}
      </aside>
      <main className="flex-1 min-w-0 px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8 overflow-y-auto">
        <Skeleton className="h-5 w-28" />
        <div className="flex flex-wrap gap-3">
          <Skeleton className="h-9 w-72 rounded-md" />
          <Skeleton className="h-9 w-[130px] rounded-md" />
          <Skeleton className="h-9 w-[120px] rounded-md" />
        </div>
        <div className="grid gap-8 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
            <Skeleton key={i} className="h-36 w-full rounded-xl" />
          ))}
        </div>
      </main>
    </div>
  );
}
