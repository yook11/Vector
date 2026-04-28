import type { Metadata } from "next";
import { Suspense } from "react";
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
          <div className="h-5 w-32 rounded bg-muted/60 animate-pulse" />
          <div className="h-3 w-64 rounded bg-muted/40 animate-pulse" />
        </div>
        <div className="h-9 w-28 rounded-md bg-muted/50 animate-pulse" />
      </div>
      <div className="space-y-2">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-12 rounded-md bg-muted/30 animate-pulse" />
        ))}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl flex flex-col gap-8 px-8 sm:px-12 py-6 sm:py-8">
        <div>
          <h1 className="text-base font-medium">Settings</h1>
          <p className="text-xs text-muted-foreground mt-2">
            Manage your news sources and application settings.
          </p>
        </div>
        <Suspense fallback={<SourceManagerSkeleton />}>
          <SourceManagerAsync />
        </Suspense>
      </div>
    </div>
  );
}
