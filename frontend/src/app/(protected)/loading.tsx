import { Skeleton } from "@/components/ui/skeleton";

export default function DashboardLoading() {
  return (
    <div className="flex">
      <aside className="hidden lg:block w-64 border-r min-h-[calc(100vh-3.5rem)] p-4 space-y-2">
        <Skeleton className="h-5 w-20 mb-4" />
        {Array.from({ length: 6 }).map((_, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
          <Skeleton key={i} className="h-8 w-full" />
        ))}
      </aside>
      <main className="flex-1 p-6 space-y-6">
        <div className="flex items-center justify-between">
          <Skeleton className="h-8 w-40" />
          <Skeleton className="h-9 w-24" />
        </div>
        <div className="flex gap-3">
          <Skeleton className="h-9 w-[140px]" />
          <Skeleton className="h-9 w-[160px]" />
          <Skeleton className="h-9 w-[130px]" />
        </div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
            <Skeleton key={i} className="h-48 w-full rounded-xl" />
          ))}
        </div>
      </main>
    </div>
  );
}
