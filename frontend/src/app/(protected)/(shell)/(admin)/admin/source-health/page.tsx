import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";
import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getSourceHealthViewModel,
  resolveWindow,
  SourceHealthView,
  SourceHealthWindowSelect,
  type WindowOption,
} from "@/features/source-health";
import { requireAdmin } from "@/lib/auth/guards";

export const metadata: Metadata = {
  title: "Source Health | Vector",
};

interface SourceHealthPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

async function SourceHealthContent({
  windowOption,
}: {
  windowOption: WindowOption;
}) {
  // DAL gate (admin): (admin) layout の認可は PPR の別 prerender 単位を守らない
  // ため、データ取得の前にここで admin 境界を明示する。非 admin は requireAdmin
  // が / へ redirect する。
  await requireAdmin();
  // build-time prerender を opt out し、admin 依存の最新 snapshot を runtime fill
  // に倒す (取得は cache: "no-store")。
  await connection();
  const data = await getSourceHealthViewModel(windowOption);
  return <SourceHealthView data={data} />;
}

function SourceHealthSkeleton() {
  return (
    <div className="space-y-2" aria-hidden="true">
      {Array.from({ length: 6 }).map((_, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
        <Skeleton key={i} className="h-14 w-full" />
      ))}
    </div>
  );
}

export default async function SourceHealthPage({
  searchParams,
}: SourceHealthPageProps) {
  const raw = await searchParams;
  const windowOption = resolveWindow(raw.window);

  return (
    <PageContainer maxWidth="4xl">
      <div>
        <h1 className="text-base font-medium">Source Health</h1>
        <p className="text-xs text-muted-foreground mt-2">
          Per-source acquisition and analyzable status within the selected
          window.
        </p>
        <div className="mt-3">
          <SourceHealthWindowSelect current={windowOption} />
        </div>
      </div>
      {/* window 変化のたびに key で再マウントし skeleton を再表示する。 */}
      <Suspense key={windowOption} fallback={<SourceHealthSkeleton />}>
        <SourceHealthContent windowOption={windowOption} />
      </Suspense>
    </PageContainer>
  );
}
