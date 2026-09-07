"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import { PageNavigationReset } from "@/components/layout/PageNavigation";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function BriefingError({
  error,
  unstable_retry,
}: ErrorPageProps) {
  return (
    <>
      <PageNavigationReset />
      <ErrorMessage
        title="ブリーフィング"
        description="ブリーフィングの取得に失敗しました"
        error={error}
        unstable_retry={unstable_retry}
      />
    </>
  );
}
