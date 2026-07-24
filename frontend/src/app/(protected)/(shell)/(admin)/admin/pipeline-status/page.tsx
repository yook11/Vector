import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";
import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getPipelineStatusViewModel,
  PipelineStatusView,
} from "@/features/pipeline-status";
import { requireAdmin } from "@/lib/auth/guards";

export const metadata: Metadata = {
  title: "Pipeline Status | Vector",
};

async function PipelineStatusContent() {
  // DAL gate (admin): (admin) layout の認可は PPR の別 prerender 単位を守らない
  // ため、データ取得の前にここで admin 境界を明示する。非 admin は requireAdmin
  // が / へ redirect する。
  await requireAdmin();
  // build-time prerender を opt out し、admin 依存の最新 snapshot を runtime fill
  // に倒す (取得は cache: "no-store")。
  await connection();
  const data = await getPipelineStatusViewModel();
  return <PipelineStatusView data={data} />;
}

function PipelineStatusSkeleton() {
  return (
    <>
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="text-sm font-medium text-muted-foreground"
      >
        パイプライン状況を読み込み中…
      </p>
      <div
        className="flex flex-col gap-8 motion-reduce:animate-none motion-reduce:[&_[data-slot=skeleton]]:animate-none"
        aria-hidden="true"
      >
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
          {Array.from({ length: 7 }).map((_, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
            <div key={i} className="flex flex-col gap-1">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-4 w-16" />
            </div>
          ))}
        </div>
        <div className="space-y-2">
          {Array.from({ length: 11 }).map((_, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      </div>
    </>
  );
}

export default function PipelineStatusPage() {
  return (
    <PageContainer maxWidth="4xl">
      <div>
        <h1 className="text-base font-medium">Pipeline Status</h1>
        <p className="text-xs text-muted-foreground mt-2">
          Read-only snapshot of each pipeline stage. Values reflect the latest
          observation.
        </p>
      </div>
      <Suspense fallback={<PipelineStatusSkeleton />}>
        <PipelineStatusContent />
      </Suspense>
    </PageContainer>
  );
}
