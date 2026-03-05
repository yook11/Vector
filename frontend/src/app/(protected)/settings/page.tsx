import type { Metadata } from "next";
import { SourceManager } from "@/components/sources/SourceManager";
import type { NewsSourceListResponse } from "@/types";

export const metadata: Metadata = {
  title: "Settings | Vector",
};

async function getSources(): Promise<NewsSourceListResponse> {
  const baseUrl =
    process.env.INTERNAL_API_URL ?? process.env.NEXT_PUBLIC_API_URL;
  if (!baseUrl) throw new Error("API URL not configured");

  // Server-side fetch — sources require auth, use getServerSession
  const { getServerSession } = await import("next-auth");
  const { authOptions } = await import("@/lib/auth");
  const session = await getServerSession(authOptions);

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (session?.accessToken) {
    headers.Authorization = `Bearer ${session.accessToken}`;
  }

  const res = await fetch(`${baseUrl}/sources`, {
    headers,
    cache: "no-store",
  });

  if (!res.ok) {
    return { items: [], total: 0 };
  }

  return res.json() as Promise<NewsSourceListResponse>;
}

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
