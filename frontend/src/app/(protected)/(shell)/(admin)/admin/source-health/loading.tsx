import { Skeleton } from "@/components/ui/skeleton";

export default function SourceHealthLoading() {
  return (
    <main className="mx-auto max-w-4xl p-6 space-y-6">
      <Skeleton className="h-8 w-40" />
      <div className="space-y-3">
        {Array.from({ length: 6 }).map((_, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    </main>
  );
}
