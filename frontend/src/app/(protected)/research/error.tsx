"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import { PageNavigationReset } from "@/components/layout/PageNavigation";
import { ResearchRouteRejectedOutcome } from "@/features/research-client";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function ResearchError({
  error,
  unstable_retry,
}: ErrorPageProps) {
  return (
    <>
      <ResearchRouteRejectedOutcome />
      <PageNavigationReset />
      <ErrorMessage
        title="Researchの読み込みに失敗しました"
        description="しばらく経ってから再度お試しください"
        error={error}
        unstable_retry={unstable_retry}
      />
    </>
  );
}
