import type { Metadata } from "next";
import { SourceManager } from "@/components/sources/SourceManager";
import { getSources } from "@/lib/api-client";

export const metadata: Metadata = {
  title: "Settings | Vector",
};

export default async function SettingsPage() {
  const sourcesData = await getSources();

  return (
    <div className="mx-auto max-w-4xl space-y-8 px-4 py-8">
      <div>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-muted-foreground">
          Manage your news sources and application settings.
        </p>
      </div>
      <SourceManager initialSources={sourcesData.items} />
    </div>
  );
}
