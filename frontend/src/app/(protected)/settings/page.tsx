import type { Metadata } from "next";
import { SourceManager } from "@/components/sources/SourceManager";
import { getSources } from "@/lib/api-client";

export const metadata: Metadata = {
  title: "Settings | Vector",
};

export default async function SettingsPage() {
  const sourcesData = await getSources();

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl flex flex-col gap-8 px-8 sm:px-12 py-6 sm:py-8">
        <div>
          <h1 className="text-base font-medium">Settings</h1>
          <p className="text-xs text-muted-foreground mt-2">
            Manage your news sources and application settings.
          </p>
        </div>
        <SourceManager initialSources={sourcesData.items} />
      </div>
    </div>
  );
}
