"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import { PageNavigationReset } from "@/components/layout/PageNavigation";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function PublicError({
  error,
  retry,
}: {
  error: ErrorPageProps["error"];
  retry: () => void;
}) {
  return (
    <>
      <PageNavigationReset />
      <ErrorMessage
        title="ページの読み込みに失敗しました"
        description="しばらく経ってから再度お試しください"
        error={error}
        unstable_retry={retry}
      />
    </>
  );
}
