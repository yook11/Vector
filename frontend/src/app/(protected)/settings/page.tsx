import { getKeywordCategories, getKeywords, getSubscriptions } from "@/lib/api-client";
import { KeywordTable } from "@/components/keywords/KeywordTable";
import { AddKeywordDialog } from "@/components/keywords/AddKeywordDialog";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Settings | Vector",
};

export default async function SettingsPage() {
  const [data, subs, kwCats] = await Promise.all([
    getKeywords(),
    getSubscriptions().catch(() => ({ items: [] })),
    getKeywordCategories().catch(() => ({ items: [] })),
  ]);

  const subscribedKeywordIds = new Set(subs.items.map((s) => s.keywordId));

  return (
    <main className="mx-auto max-w-3xl p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Settings</h1>
        <AddKeywordDialog keywordCategories={kwCats.items} />
      </div>
      <KeywordTable
        keywords={data.items}
        subscribedKeywordIds={subscribedKeywordIds}
      />
    </main>
  );
}
