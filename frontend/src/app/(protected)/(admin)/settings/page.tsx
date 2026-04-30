import type { Metadata } from "next";
import { Suspense } from "react";
import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";
import { getSources, SourceManager } from "@/features/sources";

export const metadata: Metadata = {
  title: "Settings | Vector",
};

async function SourceManagerAsync() {
  const sourcesData = await getSources();
  return <SourceManager initialSources={sourcesData.items} />;
}

function SourceManagerSkeleton() {
  return (
    <div className="space-y-4" aria-hidden="true">
      <div className="flex items-center justify-between">
        <div className="space-y-2">
          <Skeleton className="h-5 w-32" />
          <Skeleton className="h-3 w-64" />
        </div>
        <Skeleton className="h-9 w-28" />
      </div>
      <div className="space-y-2">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-12" />
        ))}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <PageContainer maxWidth="4xl">
      <div>
        <h1 className="text-base font-medium">Settings</h1>
        <p className="text-xs text-muted-foreground mt-2">
          Manage your news sources and application settings.
        </p>
      </div>
      <Suspense fallback={<SourceManagerSkeleton />}>
        <SourceManagerAsync />
      </Suspense>
    </PageContainer>
  );
}
